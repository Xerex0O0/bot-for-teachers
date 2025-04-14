import logging
import sqlite3
import qrcode
import time
import hashlib
from io import BytesIO
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ConversationHandler, CallbackContext, filters, ChatMemberHandler, CallbackQueryHandler
from datetime import datetime, timedelta
import random
import re

TOKEN = 'You token'

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

teachers = {
    'teacher1': 'password1',
    'teacher2': 'password2',
    'teacher3': 'password3',
}

AUTHORIZED_TEACHERS = {}
QR_CODES = {}

REGISTRATION_TYPE, STUDENT_NAME, STUDENT_CLASS, TEACHER_CREDENTIALS = range(4)

login_attempts = {}
LOGIN_ATTEMPT_LIMIT = 5
LOCKOUT_TIME = timedelta(minutes=5)

# Anti-Spam settings
MESSAGE_INTERVAL = 2  # seconds
user_last_message = {}

# New user verification
NEW_USER_GREETING = "Добро пожаловать! Пожалуйста, решите пример:"
user_pending_verification = {}
VERIFICATION_TIMEOUT = 60  # seconds

# Edit student states
EDIT_STUDENT_SELECT_STUDENT, EDIT_STUDENT_SELECT_FIELD, EDIT_STUDENT_ENTER_VALUE = range(3)


def create_db():
    conn = sqlite3.connect('students.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_name TEXT UNIQUE
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE,
            full_name TEXT,
            class_id INTEGER,
            language_code TEXT,  -- Добавлен столбец для языка
            registration_date TEXT, -- Добавлен столбец для даты регистрации
            FOREIGN KEY (class_id) REFERENCES classes (id) ON DELETE CASCADE
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER,
            timestamp TEXT,
            qr_code TEXT,
            FOREIGN KEY (student_id) REFERENCES students (id) ON DELETE CASCADE
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    # Таблица для хранения настроек учителей
    c.execute('''
        CREATE TABLE IF NOT EXISTS teacher_settings (
            teacher_id TEXT,
            setting_key TEXT,
            setting_value TEXT,
            PRIMARY KEY (teacher_id, setting_key)
        )
    ''')
    # Таблица для хранения авторизованных учителей
    c.execute('''
        CREATE TABLE IF NOT EXISTS authorized_teachers (
            user_id INTEGER PRIMARY KEY,
            teacher_id TEXT
        )
    ''')
    conn.commit()
    conn.close()

    set_default_qr_duration(7 * 60)


def set_default_qr_duration(duration: int):
    conn = sqlite3.connect('students.db')
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)
    ''', ('qr_duration', str(duration)))
    conn.commit()
    conn.close()


def get_qr_duration() -> int:
    conn = sqlite3.connect('students.db')
    c = conn.cursor()
    c.execute('SELECT value FROM settings WHERE key=?', ('qr_duration',))
    result = c.fetchone()
    conn.close()
    return int(result[0] if result else 7 * 60)


def generate_qr_code(content: str) -> (str, BytesIO):
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=2
        )
        qr.add_data(content)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')

        file_buffer = BytesIO()
        img.save(file_buffer, format='PNG')
        file_buffer.seek(0)

        return "QR-код сгенерирован!", file_buffer
    except Exception as e:
        logger.error(f"Error generating QR code: {e}")
        return None, None

def get_teacher_setting(teacher_id: str, setting_key: str, default_value=None):
    """Получает настройку учителя из базы данных."""
    conn = sqlite3.connect('students.db')
    c = conn.cursor()
    c.execute('SELECT setting_value FROM teacher_settings WHERE teacher_id=? AND setting_key=?', (teacher_id, setting_key))
    result = c.fetchone()
    conn.close()
    return result[0] if result else default_value

def set_teacher_setting(teacher_id: str, setting_key: str, setting_value):
    """Устанавливает настройку учителя в базе данных."""
    conn = sqlite3.connect('students.db')
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO teacher_settings (teacher_id, setting_key, setting_value) VALUES (?, ?, ?)
    ''', (teacher_id, setting_key, str(setting_value)))
    conn.commit()
    conn.close()

def add_authorized_teacher(user_id: int, teacher_id: str):
    """Добавляет учителя в список авторизованных."""
    conn = sqlite3.connect('students.db')
    c = conn.cursor()
    try:
        c.execute('INSERT INTO authorized_teachers (user_id, teacher_id) VALUES (?, ?)', (user_id, teacher_id))
        conn.commit()
    except sqlite3.IntegrityError:
        # Если учитель уже есть, ничего не делаем
        pass
    finally:
        conn.close()

def remove_authorized_teacher(user_id: int):
    """Удаляет учителя из списка авторизованных."""
    conn = sqlite3.connect('students.db')
    c = conn.cursor()
    c.execute('DELETE FROM authorized_teachers WHERE user_id=?', (user_id,))
    conn.commit()
    conn.close()

def load_authorized_teachers():
    """Загружает список авторизованных учителей из базы данных."""
    authorized_teachers = {}
    conn = sqlite3.connect('students.db')
    c = conn.cursor()
    c.execute('SELECT user_id, teacher_id FROM authorized_teachers')
    rows = c.fetchall()
    conn.close()
    for user_id, teacher_id in rows:
        authorized_teachers[user_id] = teacher_id
    return authorized_teachers

def get_main_menu_keyboard():
    keyboard = [
        [KeyboardButton('Список студентов'), KeyboardButton('Посещаемость')],
        [KeyboardButton('Время QR-кода'), KeyboardButton('Создать QR')],
        [KeyboardButton('Изменить время QR-кода'), KeyboardButton('Посещаемость за последний QR-код')],
        [KeyboardButton('Выйти')],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def show_menu(update: Update, context: CallbackContext):
    await update.message.reply_text("Вот ваше меню:", reply_markup=get_main_menu_keyboard())


async def send_startup_message(context: CallbackContext):
    """Отправляет приветственное сообщение всем зарегистрированным пользователям."""
    conn = sqlite3.connect('students.db')
    c = conn.cursor()
    c.execute('SELECT tg_id, full_name FROM students')  # Получаем tg_id и full_name
    registered_users = c.fetchall()
    conn.close()

    for user_id, full_name in registered_users:  # Обрабатываем оба значения
        try:
            first_name = full_name.split()[1] if len(full_name.split()) >= 2 else full_name.split()[0] # Получаем имя из ФИО
            greeting = f"Привет, {first_name}!"  # Создаем приветствие
            await context.bot.send_message(
                chat_id=user_id,
                text=f"{greeting} Бот был перезапущен. Ваша регистрация подтверждена."  # Настраиваем это сообщение
            )
            logger.info(f"Startup message sent to user {user_id}")
        except Exception as e:
            logger.error(f"Failed to send startup message to user {user_id}: {e}")


async def start(update: Update, context: CallbackContext) -> int:
    """Начинает диалог регистрации или пропускает его, если пользователь уже зарегистрирован."""
    user_id = update.message.from_user.id
    first_name = update.message.from_user.first_name
    last_name = update.message.from_user.last_name

    # Создаем приветствие, используя first_name и last_name
    if last_name:
        greeting = f"Привет, {first_name} {last_name}!"
    else:
        greeting = f"Привет, {first_name}!"

    conn = sqlite3.connect('students.db')
    c = conn.cursor()
    c.execute('SELECT id FROM students WHERE tg_id=?', (user_id,))
    student = c.fetchone()
    conn.close()

    if student:
        await update.message.reply_text(f"{greeting} Вы уже зарегистрированы в системе.")
        return ConversationHandler.END  # Завершаем ConversationHandler
    else:
        start_message = f"{greeting} Пожалуйста, выберите ваш статус: ученик или учитель."
        keyboard = [
            [KeyboardButton("Ученик"), KeyboardButton("Учитель")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(start_message, reply_markup=reply_markup)
        return REGISTRATION_TYPE


async def registration_type(update: Update, context: CallbackContext) -> int:
    user_type = update.message.text.lower()

    if user_type == "учитель":
        await update.message.reply_text(
            "Пожалуйста, введите ваш идентификатор и пароль в формате: <teacher_id> <password>")
        return TEACHER_CREDENTIALS
    elif user_type == "ученик":
        await update.message.reply_text("Пожалуйста, введите ваше ФИО:")
        return STUDENT_NAME
    else:
        await update.message.reply_text("Неверный выбор. Пожалуйста, выберите 'Ученик' или 'Учитель'.")
        return REGISTRATION_TYPE


async def student_name(update: Update, context: CallbackContext) -> int:
    full_name = update.message.text.strip()  # Удаляем лишние пробелы
    split_name = full_name.split()

    if len(split_name) != 3:
        await update.message.reply_text(
            "Неверный формат ФИО. Пожалуйста, введите три слова: Фамилию, Имя и Отчество."
        )
        return STUDENT_NAME

    name_pattern = r"^[А-Яа-яЁё]+$"

    for name_part in split_name:
        if not re.match(name_pattern, name_part):
            await update.message.reply_text(
                "Неверный формат ФИО. Пожалуйста, используйте только русские буквы."
            )
            return STUDENT_NAME

    context.user_data['full_name'] = full_name
    await update.message.reply_text("Пожалуйста, введите ваш класс:")
    return STUDENT_CLASS


async def student_class(update: Update, context: CallbackContext) -> int:
    class_name = update.message.text
    full_name = context.user_data.get('full_name')
    tg_id = update.message.from_user.id
    language_code = update.message.from_user.language_code  # Получаем язык
    registration_date = datetime.now().isoformat()  # Получаем дату регистрации

    conn = sqlite3.connect('students.db')
    c = conn.cursor()

    try:
        c.execute('SELECT id FROM classes WHERE class_name=?', (class_name,))
        class_id = c.fetchone()
        if not class_id:
            c.execute('INSERT INTO classes (class_name) VALUES (?)', (class_name,))
            conn.commit()
            class_id = c.lastrowid
        else:
            class_id = class_id[0]

        try:
            c.execute(
                'INSERT INTO students (tg_id, full_name, class_id, language_code, registration_date) VALUES (?, ?, ?, ?, ?)',
                (tg_id, full_name, class_id, language_code, registration_date)
            )
            conn.commit()
            await update.message.reply_text(f'{full_name} ({class_name}) успешно зарегистрирован.')
        except sqlite3.IntegrityError:
            await update.message.reply_text(f'Пользователь с таким Telegram ID уже зарегистрирован.')
    except Exception as e:
        logger.error(f"Error while processing student class: {e}")
        await update.message.reply_text("Произошла ошибка при обработке данных. Пожалуйста, попробуйте еще раз.")
    finally:
        conn.close()

    return ConversationHandler.END


async def teacher_credentials(update: Update, context: CallbackContext) -> int:
    user_id = update.message.from_user.id
    current_time = datetime.now()

    if user_id in login_attempts:
        if login_attempts[user_id]['count'] >= LOGIN_ATTEMPT_LIMIT:
            if current_time < login_attempts[user_id]['lockout_until']:
                await update.message.reply_text(
                    f"Слишком много неудачных попыток. Попробуйте снова через "
                    f"{(login_attempts[user_id]['lockout_until'] - current_time).seconds // 60} минут."
                )
                return TEACHER_CREDENTIALS
            else:
                login_attempts[user_id]['count'] = 0
                login_attempts[user_id]['lockout_until'] = current_time

    credentials = update.message.text.split()
    if len(credentials) != 2:
        await update.message.reply_text("Пожалуйста, введите идентификатор и пароль в правильном формате.")
        return TEACHER_CREDENTIALS

    teacher_id, password = credentials
    if teachers.get(teacher_id) == password:
        #  AUTHORIZED_TEACHERS[user_id] = teacher_id  Удаляем эту строку
        add_authorized_teacher(user_id, teacher_id) # Добавляем в базу данных
        AUTHORIZED_TEACHERS[user_id] = teacher_id
        login_attempts.pop(user_id, None)  # Сброс после успешного входа
        await update.message.reply_text(f"Учитель {teacher_id} успешно вошел в систему.")
        await show_menu(update, context)
        return ConversationHandler.END
    else:
        if user_id not in login_attempts:
            login_attempts[user_id] = {'count': 0, 'lockout_until': current_time}
        login_attempts[user_id]['count'] += 1
        if login_attempts[user_id]['count'] >= LOGIN_ATTEMPT_LIMIT:
            login_attempts[user_id]['lockout_until'] = current_time + LOCKOUT_TIME

        remaining_attempts = LOGIN_ATTEMPT_LIMIT - login_attempts[user_id]['count']
        await update.message.reply_text(
            f"Неверный идентификатор или пароль. Попробуйте ещё раз. Осталось попыток: {remaining_attempts}"
        )
        return TEACHER_CREDENTIALS


async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("Операция отменена.")
    context.user_data.clear()
    return ConversationHandler.END


def is_teacher(update: Update):
    return update.message.from_user.id in AUTHORIZED_TEACHERS


def teacher_only(func):
    async def wrapper(update: Update, context: CallbackContext):
        if is_teacher(update):
            return await func(update, context)
        else:
            await update.message.reply_text("Эта команда доступна только для учителей.")

    return wrapper


async def anti_spam(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    current_time = time.time()

    if user_id in user_last_message:
        time_since_last_message = current_time - user_last_message[user_id]
        if time_since_last_message < MESSAGE_INTERVAL:
            await update.message.delete()
            await context.bot.send_message(
                chat_id=update.message.chat_id,
                text="Пожалуйста, не спамьте! Подождите несколько секунд."
            )
            return True

    user_last_message[user_id] = current_time
    return False


async def generate_verification_task():
    operation = random.choice(['+', '-', '*'])
    if operation == '+':
        num1 = random.randint(1, 20)
        num2 = random.randint(1, 20)
        correct_answer = num1 + num2
        question = f"{num1} + {num2} = ?"
    elif operation == '-':
        num1 = random.randint(10, 30)
        num2 = random.randint(1, num1)
        correct_answer = num1 - num2
        question = f"{num1} - {num2} = ?"
    else:  # '*'
        num1 = random.randint(1, 10)
        num2 = random.randint(1, 10)
        correct_answer = num1 * num2
        question = f"{num1} * {num2} = ?"
    return question, str(correct_answer)


async def greet_new_member(update: Update, context: CallbackContext):
    for new_member in update.message.new_chat_members:
        user_id = new_member.id

        question, correct_answer = await generate_verification_task()
        user_pending_verification[user_id] = {
            'correct_answer': correct_answer,
            'timestamp': time.time()
        }

        await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=f"{NEW_USER_GREETING} {question}"
        )


async def verify_user(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id

    if user_id in user_pending_verification:
        user_data = user_pending_verification[user_id]
        correct_answer = user_data['correct_answer']
        timestamp = user_data['timestamp']

        if time.time() - timestamp > VERIFICATION_TIMEOUT:
            del user_pending_verification[user_id]
            await update.message.reply_text("Время для подтверждения истекло. Пожалуйста, попробуйте присоединиться к группе заново, чтобы пройти проверку.")
            await context.bot.kick_chat_member(update.message.chat_id, user_id)
            return True

        if update.message.text.strip() == correct_answer:
            await update.message.reply_text("Спасибо за подтверждение!")
            del user_pending_verification[user_id]
            return False
        else:
            await update.message.delete()
            await update.message.reply_text("Неправильный ответ. Пожалуйста, попробуйте еще раз.")
            return True
    return False


@teacher_only
async def list_students(update: Update, context: CallbackContext):
    conn = sqlite3.connect('students.db')
    c = conn.cursor()
    c.execute('''
        SELECT s.full_name, c.class_name
        FROM students s
        JOIN classes c ON s.class_id = c.id
    ''')
    students = c.fetchall()
    conn.close()

    if students:
        response = "\n".join(f"{name} ({class_name})" for name, class_name in students)
        await update.message.reply_text(f"Список всех студентов:\n{response}")
    else:
        await update.message.reply_text("Студентов не найдено.")


@teacher_only
async def list_attendance(update: Update, context: CallbackContext):
    conn = sqlite3.connect('students.db')
    c = conn.cursor()
    c.execute('''
        SELECT s.full_name, c.class_name, a.timestamp
        FROM attendance a
        JOIN students s ON a.student_id = s.id
        JOIN classes c ON s.class_id = c.id
        WHERE a.timestamp >= datetime('now', '-1 day')
    ''')
    attendance_records = c.fetchall()
    conn.close()

    if attendance_records:
        response = "\n".join(
            f"{name} ({class_name}) - {timestamp}" for name, class_name, timestamp in attendance_records)
        await update.message.reply_text(f"Посещаемость за последние сутки:\n{response}")
    else:
        await update.message.reply_text("Посещаемость за последние сутки не найдена.")


@teacher_only
async def attendance_by_last_qr(update: Update, context: CallbackContext):
    last_qr_content = max(QR_CODES, key=QR_CODES.get, default=None)
    if not last_qr_content:
        await update.message.reply_text("Нет доступных QR-кодов.")
        return

    conn = sqlite3.connect('students.db')
    c = conn.cursor()
    c.execute('''
        SELECT s.full_name, c.class_name, a.timestamp
        FROM attendance a
        JOIN students s ON a.student_id = s.id
        JOIN classes c ON s.class_id = c.id
        WHERE a.qr_code = ?
    ''', (last_qr_content,))
    attendance_records = c.fetchall()
    conn.close()

    if attendance_records:
        response = "\n".join(
            f"{name} ({class_name}) - {timestamp}" for name, class_name, timestamp in attendance_records)
        await update.message.reply_text(f"Посещаемость по последнему QR-коду:\n{response}")
    else:
        await update.message.reply_text("Посещаемость по последнему QR-коду не найдена.")


@teacher_only
async def edit_student(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("Пожалуйста, введите Telegram ID студента, которого вы хотите редактировать:")
    return EDIT_STUDENT_SELECT_STUDENT


async def edit_student_select_student(update: Update, context: CallbackContext) -> int:
    try:
        tg_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Неверный Telegram ID. Пожалуйста, введите число.")
        return EDIT_STUDENT_SELECT_STUDENT

    conn = sqlite3.connect('students.db')
    c = conn.cursor()
    c.execute('SELECT id, full_name, class_id FROM students WHERE tg_id=?', (tg_id,))
    student = c.fetchone()
    conn.close()

    if not student:
        await update.message.reply_text("Студент с таким Telegram ID не найден.")
        return ConversationHandler.END

    context.user_data['student_id'] = student[0]
    context.user_data['student_name'] = student[1]
    context.user_data['student_class_id'] = student[2]

    keyboard = [
        [InlineKeyboardButton("Имя", callback_data="edit_name"),
         InlineKeyboardButton("Класс", callback_data="edit_class")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Что вы хотите редактировать у студента {student[1]}?",
        reply_markup=reply_markup
    )
    return EDIT_STUDENT_SELECT_FIELD


async def edit_student_select_field(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()

    field = query.data
    context.user_data['edit_field'] = field

    if field == "edit_name":
        await query.edit_message_text("Пожалуйста, введите новое имя для студента:")
    elif field == "edit_class":
        await query.edit_message_text("Пожалуйста, введите новое название класса для студента:")
    return EDIT_STUDENT_ENTER_VALUE


async def edit_student_enter_value(update: Update, context: CallbackContext) -> int:
    new_value = update.message.text
    field = context.user_data['edit_field']
    student_id = context.user_data['student_id']

    conn = sqlite3.connect('students.db')
    c = conn.cursor()

    try:
        if field == "edit_name":
            c.execute('UPDATE students SET full_name=? WHERE id=?', (new_value, student_id))
            conn.commit()
            await update.message.reply_text(f"Имя студента успешно изменено на {new_value}.")
        elif field == "edit_class":
            c.execute('SELECT id FROM classes WHERE class_name=?', (new_value,))
            class_id = c.fetchone()
            if not class_id:
                c.execute('INSERT INTO classes (class_name) VALUES (?)', (new_value,))
                conn.commit()
                class_id = c.lastrowid
            else:
                class_id = class_id[0]

            c.execute('UPDATE students SET class_id=? WHERE id=?', (class_id, student_id))
            conn.commit()
            await update.message.reply_text(f"Класс студента успешно изменен на {new_value}.")
    except Exception as e:
        logger.error(f"Error while editing student: {e}")
        await update.message.reply_text("Произошла ошибка при редактировании студента. Пожалуйста, попробуйте еще раз.")
    finally:
        conn.close()
        context.user_data.clear()
        return ConversationHandler.END

@teacher_only
async def change_password(update: Update, context: CallbackContext):
    await update.message.reply_text("Функция смены пароля ещё не реализована.")

@teacher_only
async def register_student(update: Update, context: CallbackContext):
    await start(update, context)

@teacher_only
async def rename_teacher(update: Update, context: CallbackContext):
    await update.message.reply_text("Функция смены имени учителя ещё не реализована.")

@teacher_only
async def qr_duration(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    teacher_id = AUTHORIZED_TEACHERS[user_id]
    duration = get_teacher_setting(teacher_id, 'qr_duration')
    if duration is None:
        duration = get_qr_duration()
    else:
        duration = int(duration)

    await update.message.reply_text(f"Текущая длительность QR-кода: {duration} секунд.",
                                    reply_markup=get_main_menu_keyboard())

@teacher_only
async def set_qr_duration(update: Update, context: CallbackContext):
    try:
        new_duration = int(update.message.text.split()[1])
        user_id = update.message.from_user.id
        teacher_id = AUTHORIZED_TEACHERS[user_id]
        set_teacher_setting(teacher_id, 'qr_duration', new_duration)  # Сохраняем в базе данных
        await update.message.reply_text(f"Новая длительность QR-кода установлена: {new_duration} секунд.",
                                        reply_markup=get_main_menu_keyboard())
    except (IndexError, ValueError):
        await update.message.reply_text("Пожалуйста, введите корректное число секунд для длительности QR-кода.",
                                        reply_markup=get_main_menu_keyboard())

@teacher_only
async def generate_qr(update: Update, context: CallbackContext):
    try:
        content = f"attendance-{datetime.now().isoformat()}"
        expiration_time = datetime.now() + timedelta(seconds=get_qr_duration())
        QR_CODES[content] = expiration_time
        message, qr_image = generate_qr_code(content)
        if qr_image:
            await update.message.reply_photo(photo=qr_image, caption=message, reply_markup=get_main_menu_keyboard())
        else:
            await update.message.reply_text("Ошибка при генерации QR-кода.", reply_markup=get_main_menu_keyboard())
    except IndexError:
        await update.message.reply_text("Ошибка при генерации QR-кода.", reply_markup=get_main_menu_keyboard())


@teacher_only
async def update_qr_duration(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "Пожалуйста, введите новую длительность QR-кода в секундах, используя команду /set_qr_duration <seconds>.",
        reply_markup=get_main_menu_keyboard()
    )


@teacher_only
async def delete_student(update: Update, context: CallbackContext):
    try:
        tg_id = int(update.message.text.split()[1])
        conn = sqlite3.connect('students.db')
        c = conn.cursor()
        c.execute('DELETE FROM students WHERE tg_id=?', (tg_id,))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"Студент с Telegram ID {tg_id} удален.", reply_markup=get_main_menu_keyboard())
    except (IndexError, ValueError):
        await update.message.reply_text("Пожалуйста, введите правильный Telegram ID для удаления студента.",
                                        reply_markup=get_main_menu_keyboard())

async def exit_account(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    remove_authorized_teacher(user_id) # Удаляем из базы данных
    AUTHORIZED_TEACHERS.pop(user_id, None)
    await update.message.reply_text("Вы успешно вышли из аккаунта учителя.", reply_markup=get_main_menu_keyboard())

async def mark_attendance(update: Update, context: CallbackContext):
    if await anti_spam(update, context) or await verify_user(update, context):
        return
    qr_code = update.message.text
    if qr_code not in QR_CODES:
        await update.message.reply_text("Неверный QR-код.")
        return

    expiration_time = QR_CODES[qr_code]
    if datetime.now() > expiration_time:
        await update.message.reply_text("Срок действия QR-кода истек.")
        return

    tg_id = update.message.from_user.id
    conn = sqlite3.connect('students.db')
    c = conn.cursor()

    c.execute('SELECT id FROM students WHERE tg_id=?', (tg_id,))
    student_id = c.fetchone()

    if not student_id:
        await update.message.reply_text("Вы не зарегистрированы как студент.")
        return

    student_id = student_id[0]

    c.execute(
        'INSERT INTO attendance (student_id, timestamp, qr_code) VALUES (?, ?, ?)',
        (student_id, datetime.now().isoformat(), qr_code)
    )
    conn.commit()
    conn.close()

    await update.message.reply_text("Ваше присутствие на уроке отмечено.")

def main() -> None:
    create_db()

    # Загружаем авторизованных учителей из базы данных
    global AUTHORIZED_TEACHERS
    AUTHORIZED_TEACHERS = load_authorized_teachers()
    logger.info(f"Авторизованные учителя: {AUTHORIZED_TEACHERS}")

    application = Application.builder().token(TOKEN).build()

    # Send startup message to registered users
    application.job_queue.run_once(send_startup_message, when=0)

    registration_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            REGISTRATION_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, registration_type)],
            STUDENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, student_name)],
            STUDENT_CLASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, student_class)],
            TEACHER_CREDENTIALS: [MessageHandler(filters.TEXT & ~filters.COMMAND, teacher_credentials)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    edit_student_handler = ConversationHandler(
        entry_points=[CommandHandler('edit_student', edit_student)],
        states={
            EDIT_STUDENT_SELECT_STUDENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_student_select_student)],
            EDIT_STUDENT_SELECT_FIELD: [CallbackQueryHandler(edit_student_select_field)],
            EDIT_STUDENT_ENTER_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_student_enter_value)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    application.add_handler(registration_handler)
    application.add_handler(CommandHandler('menu', show_menu))
    application.add_handler(CommandHandler('list_students', list_students))
    application.add_handler(CommandHandler('attendance', list_attendance))
    application.add_handler(CommandHandler('attendance_by_last_qr', attendance_by_last_qr))
    application.add_handler(CommandHandler('update_qr_duration', update_qr_duration))
    application.add_handler(edit_student_handler)
    application.add_handler(CommandHandler('change_password', change_password))
    application.add_handler(CommandHandler('register_student', register_student))
    application.add_handler(CommandHandler('rename_teacher', rename_teacher))
    application.add_handler(CommandHandler('qr_duration', qr_duration))
    application.add_handler(CommandHandler('set_qr_duration', set_qr_duration))
    application.add_handler(CommandHandler('generate_qr', generate_qr))
    application.add_handler(CommandHandler('delete_student', delete_student))
    application.add_handler(CommandHandler('exit_account', exit_account))

    application.add_handler(MessageHandler(filters.Regex('^(Список студентов)$'), list_students))
    application.add_handler(MessageHandler(filters.Regex('^(Посещаемость)$'), list_attendance))
    application.add_handler(MessageHandler(filters.Regex('^(Время QR-кода)$'), qr_duration))
    application.add_handler(MessageHandler(filters.Regex('^(Создать QR)$'), generate_qr))
    application.add_handler(MessageHandler(filters.Regex('^(Изменить время QR-кода)$'), update_qr_duration))
    application.add_handler(MessageHandler(filters.Regex('^(Посещаемость за последний QR-код)$'), attendance_by_last_qr))
    application.add_handler(MessageHandler(filters.Regex('^(Выйти)$'), exit_account))

    application.add_handler(ChatMemberHandler(greet_new_member, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mark_attendance))

    # Start the Bot
    application.run_polling()

if __name__ == '__main__':
    main()