import hashlib
print(hashlib.sha256(b'abc').digest())      # 32 байта
print(hashlib.sha256(b'abc').hexdigest())   # те же 32 байта строкой из 64 hex-цифр