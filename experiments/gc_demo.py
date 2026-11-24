import gc
import weakref


class Node:
    def __init__(self, name):
        self.name = name
        self.next = None


# 1. Выключаем автоматический сборщик циклов, остаётся только подсчёт ссылок
gc.disable()

# 2. Создаём два узла и замыкаем их в кольцо друг на друга
a = Node('A')
b = Node('B')
a.next = b
b.next = a

# 3. Заводим слабую ссылку на узел A (она не увеличивает счётчик ссылок!)
weak_a = weakref.ref(a)

# 4. Удаляем все наши переменные из программы
del a
del b

# 5. Проверяем: при выключенном gc объект всё ещё висит в памяти!
print('После del (gc выключен), узел A жив?:', weak_a() is not None)

# 6. Запускаем сборщик циклов вручную и проверяем снова
collected = gc.collect()
print('Собрано объектов при gc.collect():', collected)
print('После gc.collect(), узел A жив?:', weak_a() is not None)