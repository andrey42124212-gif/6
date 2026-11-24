import time

t0 = time.perf_counter()
best_n, best_len, total_len = 1, 0, 0

for n in range(1, 5000):
    x = n
    steps = 0
    while x != 1:
        x = (x // 2) if (x % 2 == 0) else (3 * x + 1)
        steps += 1
    total_len += steps
    if steps > best_len:
        best_len = steps
        best_n = n

dt = time.perf_counter() - t0
print(f'ЛУЧШЕЕ n = {best_n}, ДЛИНА = {best_len}, СУММА = {total_len}, время = {dt:.5f} с')