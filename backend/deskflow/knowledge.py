import hashlib
import re
from pathlib import Path
from sqlalchemy import select, text, func
from docx import Document as WordDocument
from pypdf import PdfReader
from .config import settings
from .db import Session, uid
from .models import Document, DocumentVersion, Chunk, Config
from .providers import embed
from .security import redact, digest


def terms(value):
    words = re.findall(r'[A-Za-z0-9_]+|[\u4e00-\u9fff]+', value.lower())
    result = []
    for word in words:
        if re.search(r'[\u4e00-\u9fff]', word):
            result.extend(word[i:i + 2] for i in range(max(1, len(word) - 1)))
        else:
            result.append(word)
    return list(dict.fromkeys(result))[:1000]


def parse_file(path, filename):
    suffix = Path(filename).suffix.lower()
    blocks = []
    if suffix in {'.md', '.txt'}:
        title = ''
        for n, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
            if line.startswith('#'):
                title = line.lstrip('# ').strip()[:80]
            if line.strip():
                blocks.append((line.strip(), {'line': n, 'heading': title}))
    elif suffix == '.pdf':
        for page, item in enumerate(PdfReader(path).pages, 1):
            value = (item.extract_text() or '').strip()
            if not value:
                raise ValueError(f'第 {page} 页无可提取文本，可能为扫描页；本版不支持扫描 PDF')
            blocks.append((value, {'page': page}))
    elif suffix == '.docx':
        from docx.oxml.ns import qn
        from docx.text.paragraph import Paragraph
        from docx.table import Table
        doc = WordDocument(path)
        heading = ''
        for n, child in enumerate(doc.element.body, 1):
            if child.tag == qn('w:p'):
                p = Paragraph(child, doc)
                if p.style and p.style.name.startswith('Heading'):
                    heading = p.text[:80]
                if p.text.strip():
                    blocks.append((p.text.strip(), {'paragraph': n, 'heading': heading}))
            elif child.tag == qn('w:tbl'):
                table = Table(child, doc)
                rows = [' | '.join(c.text.strip() for c in row.cells) for row in table.rows if any(c.text.strip() for c in row.cells)]
                if rows:
                    blocks.append(('\n'.join(rows), {'table': n, 'heading': heading}))
        if doc.inline_shapes:
            raise ValueError('文档包含内嵌图片，本版不做图片识别；请补充图片文字后重新导入')
    else:
        raise ValueError('仅支持 Markdown、TXT、DOCX 和文本 PDF')
    if not blocks:
        raise ValueError('资料没有可解析的正文')
    return blocks


def split_blocks(blocks):
    """Keep source positions and headings; ~400 estimated tokens, <=800 including prefix."""
    def weight(s):
        return sum(1 if '\u4e00' <= c <= '\u9fff' else .3 for c in s)
    output, current, locations = [], '', []
    for value, location in blocks:
        sentences = re.split(r'(?<=[。！？；\n])', value)
        pieces = []
        for sentence in sentences:
            # Bound unusually long tokens/tables without losing any text.
            part = ''
            for char in sentence:
                part += char
                if weight(part) >= 600:
                    pieces.append(part)
                    part = ''
            if part:
                pieces.append(part)
        for piece in pieces:
            if current and (weight(current + piece) > 600 or locations[-1].get('heading') != location.get('heading') or locations[-1].get('page') != location.get('page')):
                output.append((current.strip(), {'start': locations[0], 'end': locations[-1]}))
                current, locations = '', []
            current += piece + '\n'
            locations.append(location)
            if weight(current) >= 400:
                output.append((current.strip(), {'start': locations[0], 'end': locations[-1]}))
                current, locations = '', []
    if current.strip():
        output.append((current.strip(), {'start': locations[0], 'end': locations[-1]}))
    return output


async def ingest(version_id):
    with Session.begin() as db:
        version = db.scalar(select(DocumentVersion).where(DocumentVersion.id == version_id).with_for_update())
        if not version or version.status not in {'queued', 'processing'}:
            return
        version.status, version.error = 'processing', ''
        filename = version.filename
        config = db.get(Config, 'embedding')
        profile = dict(config.value) if config else {}
    try:
        blocks = parse_file(settings().data_dir / 'uploads' / version_id, filename)
        parts = split_blocks(blocks)
        identity = digest({k: profile.get(k) for k in ('base_url', 'model')}) if profile.get('encrypted_key') else ''
        vectors = []
        if profile.get('encrypted_key'):
            for start in range(0, len(parts), 10):
                vectors.extend(await embed(profile, [p[0] for p in parts[start:start + 10]]))
        with Session.begin() as db:
            version = db.get(DocumentVersion, version_id)
            db.query(Chunk).filter(Chunk.version_id == version_id).delete()
            for i, (body, location) in enumerate(parts):
                db.add(Chunk(version_id=version_id, ordinal=i, text=body, location=location,
                             search_vector=func.to_tsvector('simple', ' '.join(terms(body))), embedding=vectors[i] if vectors else None))
            version.status = 'ready' if vectors else 'lexical_only'
            version.embedding_identity = identity
            version.error = '' if vectors else '未配置 Embedding：当前仅全文检索，可配置后重新入库'
            doc = db.get(Document, version.document_id)
            active = db.get(DocumentVersion, doc.active_version_id) if doc.active_version_id else None
            # Older background jobs must never replace a newer active version.
            if not active or version.number >= active.number:
                doc.active_version_id = version.id
    except Exception as exc:
        with Session.begin() as db:
            version = db.get(DocumentVersion, version_id)
            version.status, version.error = 'failed', redact(exc)


def active_versions(db):
    return list(db.scalars(select(Document.active_version_id).where(Document.active_version_id.is_not(None))).all())


async def search(query, version_ids=None):
    with Session() as db:
        ids = version_ids if version_ids is not None else active_versions(db)
        if not ids:
            return []
        profile_row = db.get(Config, 'embedding')
        profile = dict(profile_row.value) if profile_row else {}
        words = terms(query)
        if not words:
            return []
        # Individual tokens contain only letters, numbers, underscores or CJK; operators are constructed here.
        tsquery = ' | '.join(words[:80])
        statement = select(Chunk).where(Chunk.version_id.in_(ids), Chunk.search_vector.op('@@')(func.to_tsquery('simple', tsquery)))
        lexical = db.scalars(statement.order_by(func.ts_rank(Chunk.search_vector, func.to_tsquery('simple', tsquery)).desc()).limit(30)).all()
        vector_rows = []
        retrieval_warning = ''
        if profile.get('encrypted_key'):
            identity = digest({k: profile.get(k) for k in ('base_url', 'model')})
            compatible = list(db.scalars(select(DocumentVersion.id).where(DocumentVersion.id.in_(ids), DocumentVersion.embedding_identity == identity)).all())
            if compatible:
                try:
                    vector = (await embed(profile, [query]))[0]
                    vector_rows = db.scalars(select(Chunk).where(Chunk.version_id.in_(compatible), Chunk.embedding.is_not(None)).order_by(Chunk.embedding.cosine_distance(vector)).limit(30)).all()
                except Exception as exc:
                    retrieval_warning = f'向量检索不可用，使用全文检索：{redact(exc)}'
            else:
                retrieval_warning = '当前 Embedding 与资料向量版本不同，使用全文检索；可重新入库'
        else:
            retrieval_warning = '未配置 Embedding，本次仅全文检索'
        scores, by_id = {}, {}
        for ranking in (lexical, vector_rows):
            for rank, row in enumerate(ranking, 1):
                scores[row.id] = scores.get(row.id, 0) + 1 / (60 + rank)
                by_id[row.id] = row
        result, seen, remaining = [], set(), 8000
        for chunk_id in sorted(scores, key=scores.get, reverse=True)[:6]:
            row = by_id[chunk_id]
            neighbors = db.scalars(select(Chunk).where(Chunk.version_id == row.version_id, Chunk.ordinal.between(row.ordinal - 1, row.ordinal + 1)).order_by(Chunk.ordinal)).all()
            selected = [n for n in neighbors if n.id not in seen and n.location.get('start', {}).get('heading') == row.location.get('start', {}).get('heading') and n.location.get('start', {}).get('page') == row.location.get('start', {}).get('page')]
            if not selected or remaining <= 0:
                continue
            body = '\n'.join(n.text for n in selected)[:min(2400, remaining)]
            remaining -= len(body)
            seen.update(n.id for n in selected)
            version = db.get(DocumentVersion, row.version_id)
            document = db.get(Document, version.document_id)
            result.append({'id': row.id, 'version_id': version.id, 'document': document.name, 'version': version.number,
                           'text': body, 'location': {'start': selected[0].location['start'], 'end': selected[-1].location['end']},
                           'score': scores[chunk_id], 'retrieval': 'hybrid' if vector_rows else 'lexical', 'warning': retrieval_warning})
        return result
