import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    CallbackContext
)
from yoomoney import Quickpay, Client

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Состояния для конечного автомата
SELECTING_CATEGORY, SELECTING_PRODUCT, PROCESSING_PAYMENT = range(3)

# Админ-панель состояния
ADMIN_PANEL, ADDING_CATEGORY, ADDING_PRODUCT_NAME, ADDING_PRODUCT_PRICE, ADDING_PRODUCT_DESCRIPTION, ADDING_PRODUCT_QUANTITY = range(
    6)

# Пример списка категорий и товаров
CATEGORIES = {
    '1': {
        'name': 'Категория 1',
        'products': {
            '1': {
                'name': 'Цифровой товар 1',
                'price': 100,
                'description': 'Это уникальный текст для товара 1. Спасибо за покупку!',
                'in_stock': True,
                'quantity': 5
            },
            '2': {
                'name': 'Цифровой товар 2',
                'price': 200,
                'description': 'Это уникальный текст для товара 2. Поздравляем с покупкой!',
                'in_stock': True,
                'quantity': 3
            }
        }
    },
    '2': {
        'name': 'Категория 2',
        'products': {}
    }
}

# YooMoney OAuth токен
YOOMONEY_TOKEN = "YOUR_YOOMONEY_OAUTH_TOKEN"

# ID администратора (замените на ваш ID Telegram)
ADMIN_ID = 123456789

# Команда /start
async def start(update: Update, context: CallbackContext) -> int:
    user_id = update.message.from_user.id
    logger.info("Пользователь %s начал диалог.", user_id)

    # Формируем клавиатуру для выбора категории
    keyboard = [
        [InlineKeyboardButton(category['name'], callback_data=f"category_{category_id}")]
        for category_id, category in CATEGORIES.items()
    ]

    # Добавляем кнопку "Админ-панель" только для администратора
    if user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("Админ-панель", callback_data="admin_panel")])

    keyboard.append([InlineKeyboardButton("Отмена", callback_data="cancel")])  # Кнопка отмены

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text('Выберите категорию:', reply_markup=reply_markup)
    return SELECTING_CATEGORY

# Обработка выбора категории
async def select_category(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    logger.info("Получен запрос выбора категории: %s", query.data)
    await query.answer()

    if query.data == "back_to_categories":
        logger.info("Возврат к списку категорий.")
        return await start(update, context)

    category_id = query.data.split("_")[1]
    logger.info("Выбрана категория с ID: %s", category_id)
    category = CATEGORIES.get(category_id)

    if not category:
        logger.error("Категория не найдена: %s", category_id)
        await query.edit_message_text(text="Категория не найдена.")
        return ConversationHandler.END

    context.user_data['selected_category'] = category
    logger.info("Категория '%s' успешно выбрана.", category['name'])

    # Формируем клавиатуру для товаров
    keyboard = []
    for product_id, product in category['products'].items():
        if product['in_stock'] and product['quantity'] > 0:
            button_text = f"{product['name']} - ${product['price']} ({product['quantity']} шт.)"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"product_{product_id}")])

    if not keyboard:
        logger.warning("В категории '%s' нет доступных товаров.", category['name'])
        await query.edit_message_text(text=f"В категории '{category['name']}' нет доступных товаров.")
        return ConversationHandler.END

    keyboard.append([InlineKeyboardButton("Назад", callback_data="back_to_categories")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(text=f"Категория: {category['name']}. Выберите товар:", reply_markup=reply_markup)
    return SELECTING_PRODUCT

# Обработка выбора товара
async def select_product(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    logger.info("Получен запрос выбора товара: %s", query.data)
    await query.answer()

    product_id = query.data.split("_")[1]
    logger.info("Выбран товар с ID: %s", product_id)
    category = context.user_data.get('selected_category')
    product = category['products'].get(product_id)

    if not product or not product['in_stock'] or product['quantity'] <= 0:
        logger.warning("Товар недоступен: %s", product_id)
        await query.edit_message_text(text="К сожалению, этот товар больше не доступен.")
        return ConversationHandler.END

    context.user_data['selected_product'] = product
    context.user_data['order_label'] = f"order_{product_id}"  # Сохраняем уникальный идентификатор заказа

    # Создаем платежную ссылку через YooMoney
    try:
        quickpay = Quickpay(
            receiver="410011234567890",  # Замените на ваш кошелек YooMoney
            quickpay_form="shop",
            targets="Оплата товара",
            paymentType="SB",
            sum=product['price'],
            label=context.user_data['order_label']
        )
        payment_url = quickpay.redirected_url
        logger.info("Создана платежная ссылка: %s", payment_url)
    except Exception as e:
        logger.error("Ошибка при создании платежной ссылки: %s", str(e))
        await query.edit_message_text(text="Произошла ошибка при создании платежной ссылки. Попробуйте позже.")
        return ConversationHandler.END

    await query.edit_message_text(text=f"Вы выбрали: {product['name']} за ${product['price']}. Для оплаты перейдите по ссылке:\n{payment_url}")
    
    # Начинаем автоматическую проверку статуса платежа
    context.job_queue.run_repeating(check_payment_status, interval=30, first=0, data=context.user_data, name=str(update.effective_user.id))
    
    return PROCESSING_PAYMENT

# Автоматическая проверка статуса платежа
async def check_payment_status(context: CallbackContext):
    user_data = context.job.data
    order_label = user_data.get('order_label')
    product = user_data.get('selected_product')

    if not order_label or not product:
        logger.error("Ошибка: Не найдены данные о заказе или товаре.")
        return

    try:
        client = Client(YOOMONEY_TOKEN)
        history = client.operation_history(label=order_label)

        if history.operations:
            for operation in history.operations:
                if operation.status == "success":
                    # Уменьшаем количество товара
                    category = user_data['selected_category']
                    product_id = list(category['products'].keys())[list(category['products'].values()).index(product)]
                    category['products'][product_id]['quantity'] -= 1
                    if category['products'][product_id]['quantity'] <= 0:
                        category['products'][product_id]['in_stock'] = False

                    # Платеж успешен, отправляем сообщение пользователю
                    await context.bot.send_message(chat_id=context.job.name, text=f"Спасибо! Ваш заказ на {product['name']} успешно оплачен.")
                    await context.bot.send_message(chat_id=context.job.name, text=product['description'])  # Отправляем индивидуальный текст
                    context.job.schedule_removal()  # Удаляем задачу из очереди
                    return
    except Exception as e:
        logger.error("Ошибка при проверке статуса платежа: %s", str(e))

# Отображение админ-панели
async def admin_panel(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    logger.info("Получен запрос на открытие админ-панели.")
    await query.answer()

    # Формируем клавиатуру админ-панели
    keyboard = [
        [InlineKeyboardButton("Добавить категорию", callback_data="add_category")],
        [InlineKeyboardButton("Добавить товар", callback_data="add_product")],
        [InlineKeyboardButton("Назад", callback_data="back_to_categories")]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(text="Админ-панель:", reply_markup=reply_markup)
    return ADMIN_PANEL

# Команда добавления категории через админ-панель
async def add_category(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    logger.info("Получен запрос на добавление категории.")
    await query.answer()

    await query.edit_message_text(text="Введите название новой категории:")
    return ADDING_CATEGORY

# Обработка добавления категории
async def process_add_category(update: Update, context: CallbackContext) -> int:
    category_name = update.message.text
    logger.info("Добавление новой категории: %s", category_name)
    new_category_id = str(len(CATEGORIES) + 1)  # Генерируем новый ID категории
    CATEGORIES[new_category_id] = {
        'name': category_name,
        'products': {}
    }

    await update.message.reply_text(f"Категория '{category_name}' успешно добавлена!")
    return ConversationHandler.END

# Команда добавления товара через админ-панель
async def add_product(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    logger.info("Получен запрос на добавление товара.")
    await query.answer()

    # Формируем список категорий для выбора
    keyboard = [
        [InlineKeyboardButton(category['name'], callback_data=f"select_category_{category_id}")]
        for category_id, category in CATEGORIES.items()
    ]
    keyboard.append([InlineKeyboardButton("Назад", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(text="Выберите категорию для добавления товара:", reply_markup=reply_markup)
    return ADDING_PRODUCT_NAME

# Обработка выбора категории для добавления товара
async def select_category_for_product(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    logger.info("Получен запрос выбора категории для добавления товара: %s", query.data)
    await query.answer()

    category_id = query.data.split("_")[2]
    logger.info("Выбрана категория для добавления товара: %s", category_id)
    context.user_data['selected_category'] = category_id

    await query.edit_message_text(text="Введите название нового товара:")
    return ADDING_PRODUCT_PRICE

# Обработка добавления цены товара
async def process_add_product_price(update: Update, context: CallbackContext) -> int:
    product_name = update.message.text
    logger.info("Добавление названия товара: %s", product_name)
    context.user_data['product_name'] = product_name

    await update.message.reply_text("Введите цену товара (в долларах):")
    return ADDING_PRODUCT_DESCRIPTION

# Обработка добавления описания товара
async def process_add_product_description(update: Update, context: CallbackContext) -> int:
    try:
        price = int(update.message.text)
        logger.info("Добавление цены товара: %s", price)
        context.user_data['product_price'] = price
    except ValueError:
        logger.warning("Неверный формат цены: %s", update.message.text)
        await update.message.reply_text("Неверный формат цены. Введите число:")
        return ADDING_PRODUCT_PRICE

    await update.message.reply_text("Введите описание товара:")
    return ADDING_PRODUCT_QUANTITY

# Обработка добавления количества товара
async def process_add_product_quantity(update: Update, context: CallbackContext) -> int:
    description = update.message.text
    logger.info("Добавление описания товара: %s", description)
    context.user_data['product_description'] = description

    await update.message.reply_text("Введите количество товара в наличии:")
    return ADDING_PRODUCT_QUANTITY

# Завершение добавления товара
async def finish_add_product(update: Update, context: CallbackContext) -> int:
    try:
        quantity = int(update.message.text)
        logger.info("Добавление количества товара: %s", quantity)
    except ValueError:
        logger.warning("Неверный формат количества: %s", update.message.text)
        await update.message.reply_text("Неверный формат количества. Введите число:")
        return ADDING_PRODUCT_QUANTITY

    category_id = context.user_data['selected_category']
    product_id = str(len(CATEGORIES[category_id]['products']) + 1)  # Генерируем новый ID товара

    CATEGORIES[category_id]['products'][product_id] = {
        'name': context.user_data['product_name'],
        'price': context.user_data['product_price'],
        'description': context.user_data['product_description'],
        'in_stock': True,
        'quantity': quantity
    }

    await update.message.reply_text("Товар успешно добавлен!")
    return ConversationHandler.END

# Обработка команды /cancel или кнопки "Отмена"
async def cancel(update: Update, context: CallbackContext) -> int:
    logger.info("Пользователь отменил действие.")
    await update.message.reply_text("Действие отменено. Для начала используйте /start.")
    context.user_data.clear()  # Очищаем данные пользователя
    return ConversationHandler.END

# Обработка ошибок
async def error_handler(update: Update, context: CallbackContext):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

# Основная функция запуска бота
def main() -> None:
    # Замените 'YOUR_TELEGRAM_BOT_TOKEN' на токен, который вы получили от BotFather
    application = Application.builder().token("7879308234:AAFi9Uu-L7lOyFYCCIF0Kn4HH7sEkjLsfPM").build()

    # Конечный автомат для обработки диалога
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            SELECTING_CATEGORY: [
                CallbackQueryHandler(select_category, pattern=r"^category_\d+$"),  # Выбор категории
                CallbackQueryHandler(admin_panel, pattern="^admin_panel$"),  # Админ-панель
                CallbackQueryHandler(start, pattern="^back_to_categories$"),  # Возврат к категориям
                CallbackQueryHandler(cancel, pattern="^cancel$")  # Отмена
            ],
            SELECTING_PRODUCT: [
                CallbackQueryHandler(select_product, pattern=r"^product_\d+$"),  # Выбор товара
                CallbackQueryHandler(start, pattern="^back_to_categories$"),  # Возврат к категориям
                CallbackQueryHandler(cancel, pattern="^cancel$")  # Отмена
            ],
            PROCESSING_PAYMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cancel),  # Любое текстовое сообщение считаем отменой
                CommandHandler('start', start),  # Добавляем обработку /start внутри состояния
            ],
            ADMIN_PANEL: [
                CallbackQueryHandler(add_category, pattern="^add_category$"),  # Добавление категории
                CallbackQueryHandler(add_product, pattern="^add_product$"),  # Добавление товара
                CallbackQueryHandler(start, pattern="^back_to_categories$")  # Возврат к категориям
            ],
            ADDING_CATEGORY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_add_category)
            ],
            ADDING_PRODUCT_NAME: [
                CallbackQueryHandler(select_category_for_product, pattern=r"^select_category_\d+$"),
                CallbackQueryHandler(admin_panel, pattern="^admin_panel$")
            ],
            ADDING_PRODUCT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_add_product_price)
            ],
            ADDING_PRODUCT_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_add_product_description)
            ],
            ADDING_PRODUCT_QUANTITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, finish_add_product)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)],  # Добавляем команду /cancel
    )

    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)

    # Запуск бота
    application.run_polling()

if __name__ == '__main__':
    main()