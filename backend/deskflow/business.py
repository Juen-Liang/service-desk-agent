import httpx
from .config import settings
from .security import sign_permit


class BusinessError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


async def call(method, path, data=None):
    try:
        async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
            response = await client.request(method, settings().demo_url + path,
                                            headers={'Authorization': f'Bearer {settings().demo_service_secret}'}, json=data)
        if response.is_error:
            try:
                message = response.json().get('detail', f'业务接口 HTTP {response.status_code}')
            except ValueError:
                message = f'业务接口 HTTP {response.status_code}'
            raise BusinessError(str(message), response.status_code)
        return response.json()
    except httpx.HTTPError:
        raise BusinessError('演示业务服务暂不可用，请检查服务后继续；操作结果将在重试前核对')


async def execute(operation_id, action, params):
    try:
        return await call('GET', f'/operations/{operation_id}')
    except BusinessError as exc:
        if exc.status != 404:
            raise
    return await call('POST', '/operations', {'operation_id': operation_id, 'action': action, 'params': params,
                                            'permit': sign_permit(operation_id, action, params)})
