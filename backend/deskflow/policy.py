from .security import digest

DEFAULT_POLICY = {'max_permission_hours': 168, 'permission_resources': ['VPN_ACCESS', 'DEV_READONLY'],
                  'software_skus': ['IDE_PRO', 'DIAGRAM_TOOL'], 'approval_hours': 24,
                  'max_rounds': 8, 'max_tools': 20, 'prompt_version': 'v1'}


def check_policy(kind, fields, employee, policy):
    """This deterministic gate is authoritative. Knowledge and LLM text cannot override it."""
    if not employee.get('active'):
        return {'decision': 'manual', 'reason': '员工已停用，需由 IT 人员核实。'}
    if employee.get('privileged'):
        return {'decision': 'manual', 'reason': '特权账号不允许自动处理。'}
    if fields.get('employee_id') and fields['employee_id'] != employee['id']:
        return {'decision': 'manual', 'reason': '禁止操作他人账号，请由本人提交或转人工核验。'}
    params = {'employee_id': employee['id']}
    if kind == 'account':
        if employee.get('locked') and employee.get('lock_reason') != 'failed_login':
            return {'decision': 'manual', 'reason': '安全锁定不允许自动解除。'}
        return {'decision': 'execute', 'action': 'unlock_account', 'params': params, 'reason': '本人普通业务账号，符合登录失败解锁策略。'}
    if kind == 'permission':
        resource, hours = fields.get('resource'), fields.get('hours', 24)
        if not resource or not fields.get('purpose'):
            return {'decision': 'clarify', 'reason': '请补充申请的权限（VPN_ACCESS 或 DEV_READONLY）和使用用途。'}
        if resource not in policy['permission_resources'] or type(hours) is not int or not 1 <= hours <= policy['max_permission_hours']:
            return {'decision': 'manual', 'reason': '申请权限或期限超出允许范围，需人工核实。'}
        params.update(resource=resource, hours=hours)
        return {'decision': 'approve', 'action': 'grant_permission', 'params': params, 'reason': str(fields['purpose'])[:1000]}
    if kind == 'software':
        if not fields.get('sku') or not fields.get('purpose'):
            return {'decision': 'clarify', 'reason': '请补充软件（IDE_PRO 或 DIAGRAM_TOOL）和申请用途。'}
        if fields['sku'] not in policy['software_skus']:
            return {'decision': 'manual', 'reason': '软件不在预置授权目录中。'}
        params['sku'] = fields['sku']
        return {'decision': 'approve', 'action': 'assign_software', 'params': params, 'reason': str(fields['purpose'])[:1000]}
    if kind == 'device':
        owned = {d['id'] for d in employee.get('devices', [])}
        if fields.get('device_id') and fields['device_id'] not in owned:
            return {'decision': 'manual', 'reason': '设备不属于申请人，交由处理人员核验。'}
        return {'decision': 'manual', 'reason': '已登记设备问题，请按知识引用尝试基础检查；未解决时由 IT 人员接手并记录维修结果。'}
    return {'decision': 'clarify', 'reason': '请说明是账号解锁、临时权限、软件授权还是设备故障。'}


def plan_digest(action, params, policy_version):
    return digest({'action': action, 'params': params, 'policy_version': policy_version})
