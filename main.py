a = -6
b = 3
k = b % 32
u = a << (k)
if a<0: u = u | (2**k-1)

print(k)
print(u)