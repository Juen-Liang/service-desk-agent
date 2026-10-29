from pathlib import Path
import secrets
from cryptography.fernet import Fernet
target=Path(__file__).resolve().parents[1]/'.env.compose'
if target.exists(): raise SystemExit('配置已存在，未覆盖。')
values={k:secrets.token_urlsafe(36) for k in ['POSTGRES_PASSWORD','APP_DB_PASSWORD','DEMO_DB_PASSWORD','DEMO_SERVICE_SECRET','JWT_SECRET']}
values['ENCRYPTION_KEY']=Fernet.generate_key().decode()
values['FRONTEND_ORIGIN']='http://127.0.0.1:5178'
target.write_text('\n'.join(f'{k}={v}' for k,v in values.items())+'\n',encoding='utf-8')
print('已生成独立容器配置 .env.compose，不包含任何模型 Key。')
