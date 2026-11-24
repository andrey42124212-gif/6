#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
КВАНТ-6 — эмулятор учебной ЭВМ.  Лабораторная работа № 6.

Это ваш рабочий файл. Он уже запускается, но почти ничего не умеет:
в местах, помеченных  TODO, машину предстоит достроить вам.
Порядок работы — по главам задачника (lab6_student.html):

    глава 1  — Reader, load()                  -> python kvant.py info DEMO.kv6
    глава 2  — decode(), disassemble()         -> python kvant.py dis  DEMO.kv6
    глава 3  — wrap32, деление, стек, step()   -> python kvant.py run  DEMO.kv6 --no-limit
    глава 4  — CALL/RET, куча без сборки, PIX  -> экран вашего картриджа
    глава 5  — скрытые команды X1–X3           -> КОД-1
    глава 6  — сборщик мусора                  -> КОД-2 и послание
    глава 7  — (бонус) ускорение               -> python kvant.py bench BENCH.kv6

Проверка себя:   python test_kvant.py        (или  python test_kvant.py 3  — только глава 3)

Всё, что НЕ помечено TODO, — готовые инструменты: командная строка,
сохранение PNG, печать статистики. Их можно не трогать (но читать полезно).
"""

import hashlib
import os
import sys
import time
import matplotlib
from dataclasses import dataclass, field

# Чтобы русские буквы нормально печатались в консоли Windows.
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass


# ════════════════════════════════════════════════════════════════════════
#  Ошибки
# ════════════════════════════════════════════════════════════════════════

class LoadError(Exception):
    """Файл не является исправным картриджем."""


class VMError(Exception):
    """Машина остановилась с ошибкой (деление на ноль, пустой стек и т. п.)."""


# ════════════════════════════════════════════════════════════════════════
#  ГЛАВА 1.  Загрузчик
# ════════════════════════════════════════════════════════════════════════

MAGIC = b'KVNT'


@dataclass
class Cartridge:
    """Всё, что загрузчик достал из файла."""
    owner: str                  # номер студенческого владельца
    kind: int                   # 0 — основной, 1 — бенчмарк, 2 — экзаменационный
    heap: int                   # ёмкость кучи в слотах
    nglobals: int               # сколько глобальных переменных G[...]
    entry: int                  # точка входа — адрес первой команды
    consts: list = field(default_factory=list)   # пул строк
    code: bytes = b''           # секция кода
    version: tuple = (1, 0)


class Reader:
    """Читает байты по порядку, как курсор в тексте.

    r = Reader(b'\\x00\\x2A\\xFF')
    r.u16()  -> 42      (два байта, старший вперёд)
    r.u8()   -> 255
    """

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0            # где сейчас курсор

    def take(self, n: int) -> bytes:
        """Вернуть следующие n байтов и сдвинуть курсор. Если байтов не хватает — LoadError."""
        if self.pos + n > len(self.data):
            raise LoadError(f'не хватает {n} байтов в позиции {self.pos}')
        chunk = self.data[self.pos : self.pos+n]
        self.pos += n
        return chunk

    def u8(self) -> int:
        """1 байт без знака (0…255)."""
        return self.take(1)[0]

    def u16(self) -> int:
        """2 байта без знака, big-endian."""
        return int.from_bytes(self.take(2), 'big')

    def u32(self) -> int:
        """4 байта без знака, big-endian."""
        return int.from_bytes(self.take(4), 'big')


def load(data: bytes) -> Cartridge:
    """Разобрать картридж (формат — в главе 1 задачника).

    Проверить: длину, сигнатуру KVNT, SHA-256 (последние 32 байта файла).
    При любой проблеме — raise LoadError('понятное сообщение').
    """
    if len(data) < 36: raise LoadError("Файл слишком короток для картриджа.")
    body, expected_digest = data[:-32],data[-32:]
    actual_digest = hashlib.sha256(body).digest()
    if actual_digest != expected_digest: raise LoadError("Контрольная сумма SHA-256 не совпадает.")

    r = Reader(body)
    
    # проверка сигнатуры
    magic = r.take(4)
    if magic != MAGIC: raise LoadError(f'Неверная сигнатура: ожидалось {MAGIC!r}, получено {magic!r}')
    
    # версия
    version = (r.u8(), r.u8())
    
    # владелец
    owner_len = r.u8()
    owner = r.take(owner_len).decode('utf-8')
    
    # параметры
    kind = r.u8()
    heap = r.u16()
    nglobals = r.u8()
    entry = r.u16()
    
    # пул строк
    num_consts = r.u16()
    consts = []
    for i in range(num_consts):
        s_len = r.u16()
        consts.append(r.take(s_len).decode('utf-8'))

    # код
    code_len = r.u32()
    code = r.take(code_len)

    # проверка на лишнее
    if r.pos != len(body):
        raise LoadError(f'Лишние байты после кода. Прочитано {r.pos}, длина тела {len(body)}')

    return Cartridge(owner=owner, kind=kind, heap=heap, nglobals=nglobals, entry=entry, consts=consts, code=code, version=version)


# ════════════════════════════════════════════════════════════════════════
#  ГЛАВА 2.  Команды и дизассемблер
# ════════════════════════════════════════════════════════════════════════

# Код операции -> (мнемоника, форма операндов).
# Буквы формы:  i — i32 (4 байта со знаком)   b — u8 (1 байт)
#               w — u16 (номер строки/теста)  a — u16 адрес перехода
# Например, CALL имеет форму 'ab': сначала адрес (2 байта), потом n (1 байт).
OPCODES = {
    0x00: ('NOP', ''),     0x01: ('PUSH', 'i'),  0x02: ('POP', ''),
    0x03: ('DUP', ''),     0x04: ('SWAP', ''),
    0x10: ('ADD', ''),     0x11: ('SUB', ''),    0x12: ('MUL', ''),
    0x13: ('DIV', ''),     0x14: ('MOD', ''),    0x15: ('NEG', ''),
    0x18: ('EQ', ''),      0x19: ('LT', ''),     0x1A: ('GT', ''),   0x1B: ('NOT', ''),
    0x20: ('LOAD', 'b'),   0x21: ('STORE', 'b'), 0x22: ('GLOAD', 'b'), 0x23: ('GSTORE', 'b'),
    0x28: ('JMP', 'a'),    0x29: ('JZ', 'a'),    0x2A: ('JNZ', 'a'),
    0x2B: ('CALL', 'ab'),  0x2C: ('RET', ''),    0x2D: ('HALT', ''),
    0x30: ('NEW', ''),     0x31: ('GETF', ''),   0x32: ('SETF', ''),
    0x33: ('LEN', ''),     0x34: ('MEMINFO', 'b'),
    0x38: ('PRINT', ''),   0x39: ('PRINTC', ''), 0x3A: ('PRINTS', 'w'),
    0x3B: ('PIX', ''),     0x3C: ('ASSERT', 'w'),
    0x40: ('X1', ''),      0x41: ('X2', ''),     0x42: ('X3', ''),
}

OPERAND_SIZE = {'i': 4, 'b': 1, 'w': 2, 'a': 2}


def decode(code: bytes, pc: int):
    """Расшифровать одну команду по адресу pc.

    Вернуть кортеж (мнемоника, [операнды], адрес_следующей_команды)
    или None, если такого кода операции нет в OPCODES.

    decode(bytes([0x01, 0, 0, 0, 42]), 0)  ->  ('PUSH', [42], 5)
    decode(bytes([0x2B, 0, 8, 1]), 0)      ->  ('CALL', [8, 1], 4)
    """
    # Если вышли за пределы кода - декодировать нечего
    if pc >= len(code):
        return None

    opcode = code[pc]
    if opcode not in OPCODES:
        return None

    name, form = OPCODES[opcode]
    p = pc + 1
    args = []

    for t in form:
        size = OPERAND_SIZE[t]
        chunk = code[p : p + size]

        # Если байтов не хватает до конца операнда
        if len(chunk) < size:
            return None

        # Знак нужен только для типа 'i'
        is_signed = (t == 'i')
        
        # Переводим байты в целое число
        val = int.from_bytes(chunk, byteorder='big', signed=is_signed)
        args.append(val)

        p += size

    return name, args, p


def disassemble(cart: Cartridge) -> str:
    """Листинг всего кода в формате главы 2.

    L_0008:
      0008  LOAD    0
      0010  JZ      L_0019
      006C  PRINTS  0    ; 'ПРИВЕТ, КВАНТ-6!\\n'

    Метки L_XXXX — у адресов, на которые ведут переходы/вызовы, и у точки входа.
    Неизвестный байт печатается как  ??      0x40  и разбор идёт дальше со следующего байта.
    """
    # Достаем сам байткод и точку входа из картриджа
    code = cart.code
    entry = getattr(cart, "entry", 0)
    strings = (
        getattr(cart, "consts", None)
    )

    name_to_form = {name: form for name, form in OPCODES.values()}

    # ПРОХОД 1: собираем все адреса-метки (операнды 'a' + точка входа)
    labels = set()
    if entry is not None:
        labels.add(entry)

    pc = 0
    while pc < len(code):
        res = decode(code, pc)
        if res is None:
            pc += 1
            continue

        name, args, next_pc = res
        form = name_to_form.get(name, "")
        for t, arg in zip(form, args):
            if t == "a":
                labels.add(arg)

        pc = next_pc

    # ПРОХОД 2: формируем строки листинга
    lines = []
    pc = 0
    while pc < len(code):
        cur_pc = pc

        # Метка (с пометкой точки входа, если адрес совпадает с entry)
        if cur_pc in labels:
            if cur_pc == entry:
                lines.append(f"L_{cur_pc:04X}:    ; точка входа")
            else:
                lines.append(f"L_{cur_pc:04X}:")

        res = decode(code, pc)
        if res is None:
            byte_val = code[cur_pc]
            lines.append(f"  {cur_pc:04X}  {'??':<8}0x{byte_val:02X}")
            pc += 1
            continue

        name, args, next_pc = res
        form = name_to_form.get(name, "")

        formatted_args = []
        for t, arg in zip(form, args):
            if t == "a":
                formatted_args.append(f"L_{arg:04X}")
            else:
                formatted_args.append(str(arg))

        args_str = ", ".join(formatted_args)
        line = f"  {cur_pc:04X}  {name:<8}{args_str}"

        # Комментарий для PRINTS с текстом строки из таблицы strings
        if name == "PRINTS" and args:
            idx = args[0]
            if 0 <= idx < len(strings):
                line = f"{line:<21}; {repr(strings[idx])}"

        lines.append(line)
        pc = next_pc

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════
#  ГЛАВА 3.  32-битная арифметика «как в Java»
# ════════════════════════════════════════════════════════════════════════

def wrap32(x: int) -> int:
    """Привести целое Python к 32-битному со знаком (отбросить старшие биты).

    wrap32(2**31) -> -2147483648      wrap32(65536 * 65536) -> 0
    """
    new_x = x & 0xFFFFFFFF
    if new_x >= 2**31:
        new_x -= 2**32
    return new_x

def java_div(a: int, b: int) -> int:
    """Частное, округлённое К НУЛЮ:  java_div(-7, 2) -> -3   (а -7 // 2 в Python = -4!)."""
    if b == 0: raise VMError('деление на ноль')
    if a == -2147483648 and b == -1: return -2147483648
    res = abs(a)//abs(b)
    if (a < 0) ^ (b < 0):
        return res * -1
    return res


def java_mod(a: int, b: int) -> int:
    """Остаток со знаком делимого:  a - b * java_div(a, b).   java_mod(-7, 2) -> -1."""
    if a == -2147483648 and b == -1: return 0
    return a-b*java_div(a,b)


# ════════════════════════════════════════════════════════════════════════
#  ГЛАВА 5.  Скрытые команды
# ════════════════════════════════════════════════════════════════════════
# Каждая скрытая команда — функция f(a, b) -> int, результат в 32 битах.
# Когда разгадаете команду, допишите её сюда, например:
#
#     def x1(a, b):
#         return wrap32(...)
#
#     HIDDEN = {'X1': x1}
#
# Пока команды нет в словаре, машина честно останавливается на ней
# с ошибкой «неизвестная команда 0x40».

def x1(a, b):
    return wrap32((a & 0xFFFFFFFF) >> (b % 32))

def x2(a, b):
    return wrap32(a << (b % 32))

def x3(a, b):
    u = a & 0xFFFFFFFF
    k = b % 32
    return wrap32((u << k) | (u >> (32 - k)))

HIDDEN = {'X1': x1, 'X2': x2, 'X3': x3}


# ════════════════════════════════════════════════════════════════════════
#  ГЛАВЫ 4 и 6.  Куча и сборщик мусора
# ════════════════════════════════════════════════════════════════════════

class HeapObject:
    """Массив в куче «Кванта». Ссылка на него — это сам объект Python."""
    __slots__ = ('items', 'alive', 'marked')

    def __init__(self, n: int, fill=0):
        self.items = [fill] * n     # элементы: числа или ссылки (HeapObject)
        self.alive = True           # False — объект освобождён сборщиком
        self.marked = False         # пометка для сборщика (глава 6)

    def __repr__(self):
        return f'<объект[{len(self.items)}]{"" if self.alive else " освобождён"}>'


class Heap:
    """Учёт памяти. Объект из n элементов занимает n + 1 слот."""

    def __init__(self, capacity: int, no_limit: bool = False):
        self.capacity = capacity
        self.no_limit = no_limit     # True — ёмкость игнорируется, сборки нет
        self.objects = []            # все созданные и ещё не освобождённые объекты
        self.used = 0                # занято слотов
        self.peak = 0                # максимум used за всё время
        self.gc_count = 0            # сколько раз запускалась сборка
        self.allocated = 0           # сколько объектов создано всего
        self.freed = 0               # сколько объектов освобождено всего
        self.log = []                # история для графика: (номер_команды, used, событие)

    def alloc(self, n: int, vm, fill=0) -> HeapObject:
        """Создать объект из n элементов (правила — глава 4, затем глава 6).

        vm передаётся, чтобы сборщик мог спросить у машины корни (vm.roots())
        и номер текущей команды (vm.steps) для графика.
        """

        if isinstance(n, HeapObject) or not isinstance(n, int) or not (1 <= n <= 4096): raise VMError('некорректный размер объекта')

        if not self.no_limit and self.used+n+1 > self.capacity:
            self.collect(vm)
            if self.used+n+1 > self.capacity:
                raise VMError('OUT OF MEMORY')
        
        new_obj = HeapObject(n, fill)
        self.used += (n+1)
        if self.used > self.peak: self.peak = self.used
        self.allocated += 1
        self.objects.append(new_obj)
        self.log.append((vm.steps, self.used, 'alloc'))
        return new_obj

        # TODO 4.3  Проверить n (число, 1…4096), создать HeapObject, обновить
        #           used / peak / allocated, добавить в self.objects,
        #           записать в self.log (vm.steps, self.used, 'alloc').
        # TODO 6.3  Перед созданием: если не no_limit и used + (n+1) > capacity —
        #           self.collect(vm); если места всё равно нет — VMError('OUT OF MEMORY').

    def collect(self, vm):
        """Сборка мусора «пометка и очистка» (mark & sweep). Глава 6."""
        # TODO 6.2  1) gc_count += 1, запись в log (vm.steps, used, 'gc_before')
        #           2) пометка: обойти всё достижимое из vm.roots()
        #              (без рекурсии Python — используйте список-«очередь дел»)
        #           3) очистка: непомеченные -> alive = False, вернуть их слоты,
        #              помеченным снять пометку
        #           4) запись в log (vm.steps, used, 'gc_after')
        self.gc_count += 1
        self.log.append((vm.steps, self.used, 'gc_before'))
        queue = []
        for i in vm.roots():
            if isinstance(i, HeapObject) and not i.marked:
                i.marked = True
                queue.append(i)
        while queue:
            obj = queue.pop()
            for item in obj.items:
                if isinstance(item, HeapObject) and not item.marked:
                    item.marked = True
                    queue.append(item)

        new_objects = []
        for obj in self.objects:
            if obj.marked:
                obj.marked = False
                new_objects.append(obj)
            else:
                obj.alive = False
                self.used -= len(obj.items) + 1
                self.freed += 1
        self.objects = new_objects
        self.log.append((vm.steps, self.used, 'gc_after'))


# ════════════════════════════════════════════════════════════════════════
#  ГЛАВЫ 3–6.  Машина
# ════════════════════════════════════════════════════════════════════════

STACK_LIMIT = 1024
FRAME_LIMIT = 256
NLOCALS = 16


class Frame:
    """Кадр вызова функции."""
    __slots__ = ('locals', 'ret', 'base')

    def __init__(self, ret: int, base: int):
        self.locals = [0] * NLOCALS   # L[0] … L[15]
        self.ret = ret                # адрес возврата
        self.base = base              # «дно»: высота стека в момент входа


class VM:
    def __init__(self, cart: Cartridge, hidden=None, heap_capacity=None,
                 no_limit=False, echo=False, trace=False):
        self.cart = cart
        self.code = cart.code
        self.pc = cart.entry
        self.stack = []                           # стек операндов
        self.frames = [Frame(ret=-1, base=0)]     # кадр главной программы
        self.globals = [0] * cart.nglobals
        cap = cart.heap if heap_capacity is None else heap_capacity
        self.heap = Heap(cap, no_limit)
        self.screen = [[0] * 32 for _ in range(32)]   # screen[y][x]
        self.out = []                             # всё, что напечатала программа
        self.steps = 0
        self.halted = False
        self.echo = echo                          # печатать вывод сразу
        self.trace = trace                        # печатать каждую команду (отладка)
        self.hidden = HIDDEN if hidden is None else hidden
        self.handlers = self._build_handlers()

    # ── готовые инструменты ─────────────────────────────────────────────
    def _build_handlers(self):
        """Таблица диспетчеризации: мнемоника -> метод op_<мнемоника>.

        Метод op_push обрабатывает PUSH, op_add — ADD и т. д. Если метода нет,
        команда считается неизвестной. Скрытые команды берутся из self.hidden.
        """
        table = {}
        for name, _form in OPCODES.values():
            method = getattr(self, 'op_' + name.lower(), None)
            if name in ('X1', 'X2', 'X3'):
                if name in self.hidden:
                    table[name] = self._make_hidden(self.hidden[name])
            elif method is not None:
                table[name] = method
            else:
                table[name] = self._not_yet(name)
        return table

    @staticmethod
    def _not_yet(name):
        def handler(args):
            raise NotImplementedError(f'команда {name} — нет метода op_{name.lower()}')
        return handler

    def _make_hidden(self, f):
        def handler(args):
            b = self.pop_int()
            a = self.pop_int()
            self.push(wrap32(f(a, b)))
        return handler

    def write(self, text: str):
        """Вывод программы (PRINT, PRINTC, PRINTS)."""
        self.out.append(text)
        if self.echo:
            print(text, end='', flush=True)

    def output(self) -> str:
        return ''.join(self.out)

    @property
    def frame(self) -> Frame:
        """Текущий кадр."""
        return self.frames[-1]

    def run(self, max_steps=200_000_000):
        while not self.halted:
            if self.steps >= max_steps:
                raise VMError('слишком много шагов — похоже на бесконечный цикл')
            if self.trace:
                print(self.trace_line(), file=sys.stderr)
            self.step()

    def trace_line(self) -> str:
        d = decode(self.code, self.pc)
        what = '??' if d is None else (d[0] + ' ' + ', '.join(map(str, d[1]))).strip()
        top = ', '.join(_short(v) for v in self.stack[-6:])
        return f'{self.steps:>8}  {self.pc:04X}  {what:<18} стек: [{"…, " if len(self.stack) > 6 else ""}{top}]'

    def stats(self) -> str:
        h = self.heap
        return ('=== СТАТИСТИКА ===\n'
                f'команд выполнено:     {self.steps}\n'
                f'сборок мусора:        {h.gc_count}\n'
                f'пик / ёмкость кучи:   {h.peak} / {h.capacity}\n'
                f'занято в конце:       {h.used}\n'
                f'объектов создано:     {h.allocated}\n'
                f'объектов освобождено: {h.freed}\n')

    # ── ГЛАВА 3: стек ───────────────────────────────────────────────────
    def push(self, v):
        """Положить значение на стек. Больше STACK_LIMIT — VMError('переполнение стека')."""
        if len(self.stack) > STACK_LIMIT: raise VMError('переполнение стека')
        self.stack.append(v)

    def pop(self):
        """Снять значение. Если стек не выше дна текущего кадра — VMError('стек пуст')."""
        # TODO 3.3  Дно — self.frame.base
        if (len(self.stack)-self.frame.base) < 1: raise VMError('стек пуст')
        return self.stack.pop()

    def pop_int(self) -> int:
        """Снять ЧИСЛО. Если там ссылка — VMError('ожидалось число')."""
        v = self.pop()
        if not isinstance(v, int): raise VMError('ожидалось число')
        return v

    def pop_ref(self) -> HeapObject:
        """Снять ССЫЛКУ на живой объект. Число — 'ожидалась ссылка';
        освобождённый объект — 'обращение к освобождённому объекту'."""
        v = self.pop()
        if isinstance(v, int): raise VMError('Ожидалась ссылка')
        if not v.alive: raise VMError('Обращение к освобождённому объекту')
        return v

    # ── ГЛАВА 3: главный цикл ───────────────────────────────────────────
    def step(self):
        """Выполнить одну команду: выборка -> расшифровка -> исполнение.

        1. Взять pc; расшифровать команду decode(self.code, pc).
           Нет такой команды (None) или для неё нет обработчика в self.handlers —
           VMError(f'pc={pc:04X}: неизвестная команда 0x{байт:02X}').
        2. self.steps += 1;  self.pc = адрес следующей команды
           (переходы потом сами перепишут self.pc).
        3. Вызвать обработчик: self.handlers[мнемоника](операнды).
        4. Если обработчик бросил VMError(msg), перебросить
           VMError(f'pc={pc:04X} {мнемоника}: {msg}') — чтобы было видно, где упало.
        """
        pc = self.pc
        decoded = decode(self.code, pc)
        if decoded is None: 
            byte = self.code[pc] if 0 <= pc < len(self.code) else 0
            raise VMError(f'pc={pc:04X}: неизвестная команда 0x{byte:02X}')
        name, args, next_pc = decoded
        if name not in self.handlers: raise VMError(f'pc={pc:04X}: неизвестная команда 0x{self.code[pc]:02X}')
        self.steps += 1
        self.pc = next_pc
        try:
            self.handlers[name](args)
        except VMError as e:
            raise VMError(f'pc={pc:04X} {name}: {e}') from None

    # ── обработчики команд ──────────────────────────────────────────────
    # Каждый получает список операндов args (может быть пустым).
    # Три уже написаны — по ним видно, как устроены остальные.

    def op_nop(self, args):
        pass

    def op_push(self, args):
        self.push(args[0])

    def op_prints(self, args):
        self.write(self.cart.consts[args[0]])

    def op_halt(self, args):
        self.halted = True

    def op_pop(self, args):
        self.pop()

    def op_dup(self, args):
        self.push(self.stack[-1])

    def op_swap(self, args):
        temp = self.stack[-1]
        self.stack[-1] = self.stack[-2]
        self.stack[-2] = temp

    def _binary(self, f):
        b = self.pop_int()
        a = self.pop_int()
        self.push(wrap32(f(a,b)))

    def op_add(self, args):
        self._binary(lambda a, b: a+b)

    def op_sub(self, args):
        self._binary(lambda a, b: a-b)

    def op_mul(self, args):
        self._binary(lambda a, b: a*b)

    def op_div(self, args):
        self._binary(java_div)

    def op_mod(self, args):
        self._binary(java_mod)
    
    def op_neg(self, args):
        a = self.pop()
        self.push(wrap32(-a))

    def _cmp(self, f):
        b = self.pop()
        a = self.pop()
        self.push(int(f(a,b)))

    def op_eq(self, args):
        self._cmp(lambda a, b: a==b)

    def op_lt(self, args):
        self._cmp(lambda a, b: a<b)

    def op_gt(self, args):
        self._cmp(lambda a, b: a>b)

    def op_not(self, args):
        a = self.pop()
        res = 1 if a == 0 else 0
        self.push(res)

    def op_load(self, args):
        idx = args[0]
        if 0 <= idx < len(self.frame.locals):
            self.push(self.frame.locals[idx])
        else:
            raise VMError('нет локальной переменной')

    def op_store(self, args):
        idx = args[0]
        v = self.pop()
        if 0 <= idx < len(self.frame.locals):
            self.frame.locals[idx] = v
        else:
            raise VMError('нет локальной переменной')

    def op_gload(self, args):
        idx = args[0]
        if 0 <= idx < len(self.globals):
            self.push(self.globals[idx])
        else:
            raise VMError('нет глобальной переменной')

    def op_gstore(self, args):
        idx = args[0]
        v = self.pop()
        if 0 <= idx < len(self.globals):
            self.globals[idx] = v
        else:
            raise VMError('нет глобальной переменной')

    def op_jmp(self, args):
        self.pc = args[0]

    def op_jz(self, args):
        v = self.pop()
        if v == 0: self.pc = args[0]

    def op_jnz(self, args):
        v = self.pop()
        if v != 0: self.pc = args[0]

    def op_print(self, args):
        v = self.pop()
        self.write(str(v))

    def op_printc(self, args):
        v = self.pop_int()
        self.write(chr(v))

    def op_assert(self, args):
        num = args[0]
        v = self.pop()
        if v == 0: raise VMError(f'ASSERT {args[0]} провалился')

    def op_call(self, args):
        addr, n = args
        if len(self.frames) >= FRAME_LIMIT: raise VMError('Ошибка бесконечной рекурсии')
        if not(0 <= n <= NLOCALS): raise VMError
        new_frame = Frame(ret=self.pc, base=0)
        for i in range(n-1, -1, -1): 
            new_frame.locals[i] = self.pop()
        new_frame.base=len(self.stack)
        self.frames.append(new_frame)
        self.pc = addr

    def op_ret(self, args):
        ret_val = self.pop()
        del self.stack[self.frame.base:]
        old_frame = self.frames.pop()
        if old_frame.ret == -1:
            self.halted = True
            raise VMError('RET вызван из главной программы')
        self.pc = old_frame.ret
        self.push(ret_val)
        
    def op_new(self, args):
        n = self.pop_int()
        obj = self.heap.alloc(n, self)
        self.push(obj)

    def op_getf(self, args):
        idx = self.pop_int()
        obj = self.pop_ref()
        if not (0 <= idx < len(obj.items)): raise VMError('Индекс вне диапазона')
        self.push(obj.items[idx])

    def op_setf(self, args):
        val = self.pop()
        idx = self.pop_int()
        obj = self.pop_ref()
        if not (0 <= idx < len(obj.items)): raise VMError('Индекс вне диапазона')
        obj.items[idx] = val

    def op_len(self, args):
        obj = self.pop_ref()
        self.push(len(obj.items))

    def op_meminfo(self, args):
        code = args[0]
        match code:
            case 0: self.push(self.heap.gc_count)
            case 1: self.push(self.heap.used)
            case 2: self.push(self.heap.peak)
            case 3: self.push(self.heap.capacity)
            case 4: self.push(self.heap.allocated)
            case 5: self.push(self.heap.freed)
            case 6: self.push(self.heap.gc_count)

    def op_pix(self, args):
        color = self.pop_int()
        y = self.pop_int()
        x = self.pop_int()
        if not (0 <= x < 32 and 0 <= y < 32):
            raise VMError('координаты пикселя вне экрана')
        if not (0 <= color < len(PALETTE)):
            raise VMError('некорректный цвет пикселя')
        self.screen[y][x] = color


    # TODO 3.5  Напишите остальные обработчики. Имена: op_pop, op_dup, op_swap,
    #   op_add, op_sub, op_mul, op_div, op_mod, op_neg, op_eq, op_lt, op_gt, op_not,
    #   op_load, op_store, op_gload, op_gstore, op_jmp, op_jz, op_jnz,
    #   op_print, op_printc, op_assert                                  (глава 3)
    #   op_call, op_ret, op_new, op_getf, op_setf, op_len, op_meminfo, op_pix (глава 4)
    #
    # Пример для разгона:
    #
    #   def op_add(self, args):
    #       b = self.pop_int()          # вершина — второй операнд!
    #       a = self.pop_int()
    #       self.push(wrap32(a + b))

    # ── ГЛАВА 6: корни ──────────────────────────────────────────────────
    def roots(self):
        """Все значения, с которых сборщик начинает обход:
        стек операндов, локальные ВСЕХ кадров, глобальные."""
        yield from self.stack
        yield from self.globals
        for frame in self.frames: yield from frame.locals
        # TODO 6.1  Удобно сделать генератором:  yield from self.stack  и т. д.


def _short(v):
    return f'obj[{len(v.items)}]' if isinstance(v, HeapObject) else str(v)


# ════════════════════════════════════════════════════════════════════════
#  Готовые инструменты: экран, график, командная строка
# ════════════════════════════════════════════════════════════════════════

PALETTE = [(0x00, 0x00, 0x00), (0x3b, 0x5b, 0xdb), (0xc2, 0x3b, 0x22), (0x8f, 0x3f, 0xa0),
           (0x2f, 0x9e, 0x44), (0x23, 0xa6, 0xc9), (0xe8, 0xc5, 0x47), (0xf2, 0xf2, 0xee)]


def save_png(screen, path, scale=10):
    """Сохранить экран 32×32 в PNG без сторонних библиотек (zlib + struct)."""
    import struct
    import zlib
    w = h = 32 * scale
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = screen[y // scale]
        for x in range(w):
            raw.extend(PALETTE[row[x // scale]])

    def chunk(tag, data):
        c = struct.pack('>I', len(data)) + tag + data
        return c + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(bytes(raw), 9)) + chunk(b'IEND', b''))
    with open(path, 'wb') as f:
        f.write(png)


def plot_heap(log, capacity, path):
    """График занятости кучи (глава 6). По горизонтали — номер команды, по вертикали — used."""
    # TODO 6.4  matplotlib: точки из log, линия ёмкости (axhline), моменты сборок.
    print('(график кучи ещё не написан — глава 6)')


BENCH_EXPECTED = 'БЕНЧМАРК: КОЛЛАЦ, N = 5000\nЛУЧШЕЕ n = 3711, ДЛИНА = 237\nСУММА ДЛИН = 387940\n'


def calibrate(n=1_500_000):
    """Эталонная нагрузка «как у интерпретатора»: стек, словарь, вызовы методов.
    Нужна, чтобы сравнивать скорость машин на разных ноутбуках (глава 7). Не меняйте её."""
    stack = []
    push, pop = stack.append, stack.pop
    table = {0: lambda a, b: a + b, 1: lambda a, b: a ^ b}
    x = 1
    t0 = time.perf_counter()
    for i in range(n):
        push(i)
        push(x)
        b = pop()
        a = pop()
        x = table[i & 1](a, b) & 0xFFFF
    return n / (time.perf_counter() - t0)


def read_cartridge(path):
    with open(path, 'rb') as f:
        return load(f.read())


def cmd_info(path):
    with open(path, 'rb') as f:
        data = f.read()
    c = load(data)
    kinds = {0: 'основной', 1: 'бенчмарк', 2: 'экзаменационный'}
    print(f'Файл:           {path} ({len(data)} байт)')
    print(f'Версия формата: {c.version[0]}.{c.version[1]}')
    print(f'Владелец:       {c.owner}')
    print(f'Тип:            {c.kind} ({kinds.get(c.kind, "?")})')
    print(f'Ёмкость кучи:   {c.heap} слотов')
    print(f'Глобальных:     {c.nglobals}')
    print(f'Точка входа:    0x{c.entry:04X}')
    print(f'Длина кода:     {len(c.code)} байт')
    print(f'SHA-256:        совпадает')
    print(f'Пул строк ({len(c.consts)}):')
    for i, s in enumerate(c.consts):
        print(f'  {i:>3}: {s!r}')


def cmd_dis(path, out=None):
    text = disassemble(read_cartridge(path))
    if out:
        with open(out, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f'Листинг записан в {out}')
    else:
        print(text, end='')


def cmd_run(path, no_limit=False, heap=None, out_dir='out', trace=False):
    cart = read_cartridge(path)
    vm = VM(cart, heap_capacity=heap, no_limit=no_limit, echo=True, trace=trace)
    t0 = time.perf_counter()
    try:
        vm.run()
    except VMError as e:
        print(f'\nОШИБКА VM: {e}')
    dt = time.perf_counter() - t0
    print()
    print(vm.stats(), end='')
    print(f'время:                {dt:.2f} с')
    os.makedirs(out_dir, exist_ok=True)
    png = os.path.join(out_dir, f'screen_{cart.owner}.png')
    save_png(vm.screen, png)
    print(f'Экран сохранён: {png}')
    if vm.heap.log and not no_limit:
        plot_heap(vm.heap.log, vm.heap.capacity, os.path.join(out_dir, f'heap_{cart.owner}.png'))
    return vm


def cmd_bench(path, profile=False):
    cart = read_cartridge(path)
    vm = VM(cart)
    print('Бенчмарк запущен, подождите…', flush=True)
    t0 = time.perf_counter()
    if profile:
        import cProfile
        import pstats
        prof = cProfile.Profile()
        prof.enable()
    try:
        vm.run()
    except VMError as e:
        print(f'ОШИБКА VM: {e}')
        return
    dt = time.perf_counter() - t0
    if profile:
        prof.disable()
        pstats.Stats(prof).sort_stats('tottime').print_stats(12)
    print(vm.output(), end='')
    ok = vm.output() == BENCH_EXPECTED
    speed = vm.steps / dt
    calib = calibrate()
    index = speed / calib * 1000
    print(f'\nкоманд: {vm.steps:,}   время: {dt:.2f} с   скорость: {speed:,.0f} команд/с'.replace(',', ' '))
    print(f'ответ верный: {"ДА" if ok else "НЕТ — результат не засчитывается"}')
    print(f'КВАНТ-ИНДЕКС: {index:.0f}' if ok else 'КВАНТ-ИНДЕКС: —')
    print(f'(Python {sys.version.split()[0]}; калибровка {calib:,.0f} оп/с)'.replace(',', ' '))


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog='kvant.py', description='Эмулятор ЭВМ «Квант-6»')
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('info', help='заголовок и пул строк')
    s.add_argument('file')
    s = sub.add_parser('dis', help='дизассемблер')
    s.add_argument('file')
    s.add_argument('-o', '--out')
    s = sub.add_parser('run', help='запустить картридж')
    s.add_argument('file')
    s.add_argument('--no-limit', action='store_true', help='без ограничения кучи и без сборки')
    s.add_argument('--heap', type=int, help='заменить ёмкость кучи')
    s.add_argument('--out', default='out', help='папка для PNG')
    s.add_argument('--trace', action='store_true', help='печатать каждую команду (в stderr)')
    s = sub.add_parser('bench', help='замер скорости (глава 7)')
    s.add_argument('file')
    s.add_argument('--profile', action='store_true', help='показать, где тратится время')
    a = p.parse_args(argv)
    try:
        if a.cmd == 'info':
            cmd_info(a.file)
        elif a.cmd == 'dis':
            cmd_dis(a.file, a.out)
        elif a.cmd == 'run':
            cmd_run(a.file, a.no_limit, a.heap, a.out, a.trace)
        elif a.cmd == 'bench':
            cmd_bench(a.file, a.profile)
    except LoadError as e:
        print(f'Картридж не загружен: {e}')
        sys.exit(1)
    except FileNotFoundError as e:
        print(f'Нет такого файла: {e.filename}')
        sys.exit(1)
    except NotImplementedError as e:
        print(f'\nЭта часть машины ещё не построена: {e}.\n'
              f'Найдите в kvant.py соответствующий TODO.')
        sys.exit(2)


if __name__ == '__main__':
    main()
