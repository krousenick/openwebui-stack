import jwt
from open_webui.env import WEBUI_SECRET_KEY

print('alg/key head:', WEBUI_SECRET_KEY[:8], 'len:', len(WEBUI_SECRET_KEY))
token = open('/tmp/token.txt').read().strip()
try:
    d = jwt.decode(token, WEBUI_SECRET_KEY, algorithms=['HS256'])
    print('DECODE OK:', {k: str(v) for k, v in d.items()})
except Exception as e:
    print('DECODE FAIL:', e)
