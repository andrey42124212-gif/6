data = bytearray(open('DEMO.kv6', 'rb').read())
data[200] ^= 1                      # «перевернуть» младший бит одного байта
open('broken.kv6', 'wb').write(data)