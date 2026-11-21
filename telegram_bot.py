import requests
import logging
import json
import os
from datetime import datetime
from typing import Dict, List, Optional

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class HynixTelegramBot:
    def __init__(self, bot_token, admin_chat_id, users_db_path="hynix_users.json"):
        self.bot_token = bot_token
        self.admin_chat_id = admin_chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}"
        
        # Используем абсолютный путь к файлу базы данных
        if not os.path.isabs(users_db_path):
            script_dir = os.path.dirname(os.path.abspath(__file__))
            self.users_db_path = os.path.join(script_dir, users_db_path)
        else:
            self.users_db_path = users_db_path
        
        # Загрузка базы пользователей
        self.users_db = self.load_users_db()
        
        # Последний update для polling
        self.last_update = 0

        # API статистики лоадера (необязательно)
        self.stats_api_base = (os.environ.get("HYNIX_API_URL") or os.environ.get("API_URL") or "").rstrip("/")
        self.stats_admin_token = os.environ.get("HYNIX_ADMIN_TOKEN") or ""
        
    def load_users_db(self) -> Dict:
        """Загрузка базы пользователей из JSON файла"""
        try:
            if os.path.exists(self.users_db_path):
                with open(self.users_db_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {"users": []}
        except Exception as e:
            logger.error(f"Ошибка загрузки БД: {e}")
            return {"users": []}
    
    def save_users_db(self):
        """Сохранение базы пользователей в JSON файл"""
        try:
            with open(self.users_db_path, 'w', encoding='utf-8') as f:
                json.dump(self.users_db, f, indent=2, ensure_ascii=False)
            logger.info("БД пользователей сохранена")
        except Exception as e:
            logger.error(f"Ошибка сохранения БД: {e}")
    
    def send_message(self, chat_id, message, parse_mode="HTML", reply_markup=None):
        """Отправка сообщения в Telegram"""
        try:
            url = f"{self.api_url}/sendMessage"
            data = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": parse_mode
            }
            if reply_markup:
                data["reply_markup"] = reply_markup
            response = requests.post(url, json=data, timeout=10)
            response.raise_for_status()
            logger.info(f"Сообщение отправлено в {chat_id}")
            return True
        except Exception as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")
            return False
    
    def get_updates(self, offset=0, timeout=30):
        """Получение обновлений от Telegram"""
        try:
            url = f"{self.api_url}/getUpdates"
            params = {
                "offset": offset,
                "timeout": timeout
            }
            response = requests.get(url, params=params, timeout=timeout + 5)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Ошибка получения обновлений: {e}")
            return {"ok": False, "result": []}
    
    def is_admin(self, chat_id, user_id=None):
        """Разрешаем управление только владельцу указанного личного чата."""
        effective_user_id = chat_id if user_id is None else user_id
        return (
            str(chat_id) == str(self.admin_chat_id)
            and str(effective_user_id) == str(self.admin_chat_id)
        )
    
    def process_message(self, message):
        """Обработка входящего сообщения"""
        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        username = message["from"].get("username", "unknown")
        text = message.get("text", "")
        
        logger.info(f"Сообщение от {username} (ID: {user_id}): {text}")
        
        # Бот приватный: чужим не отвечаем вообще, чтобы не палить его существование
        if not self.is_admin(chat_id, user_id):
            logger.info(f"Игнорирую сообщение от {username} (ID: {user_id}) — не админ")
            return
        
        # Обработка команд
        if text.startswith("/"):
            self.handle_admin_command(text, chat_id)
    
    def handle_admin_command(self, command, chat_id):
        """Обработка админских команд"""
        parts = command.split()
        cmd = parts[0].lower()
        
        if cmd == "/start":
            self.send_admin_panel(chat_id)
        elif cmd == "/users":
            self.show_users(chat_id)
        elif cmd == "/players":
            self.show_players(chat_id)
        elif cmd == "/online":
            self.show_online(chat_id)
        elif cmd == "/stats":
            self.show_stats(chat_id)
        elif cmd == "/createkey":
            self.create_key(chat_id, parts)
        elif cmd == "/setplan":
            self.set_user_plan(chat_id, parts)
        elif cmd == "/ban":
            self.ban_user(chat_id, parts)
        elif cmd == "/unban":
            self.unban_user(chat_id, parts)
        elif cmd == "/help":
            self.show_help(chat_id)
        elif cmd == "/extend":
            self.extend_user(chat_id, parts)
        elif cmd == "/user":
            self.show_user(chat_id, parts)
        elif cmd == "/resethwid":
            self.handle_reset_hwid_command(chat_id, parts)
        elif cmd == "/init_admin_hwid":
            self.handle_init_admin_hwid_command(chat_id, parts)
        else:
            self.send_message(chat_id, f"❌ Неизвестная команда: {cmd}\n\nИспользуйте /help для списка команд")
    
    def handle_reset_hwid_command(self, chat_id, parts):
        if len(parts) < 2:
            self.send_message(chat_id, "❌ Использование: /resethwid [user_id]")
            return
        user_id = parts[1]
        success, message = self.reset_hwid(user_id)
        self.send_message(chat_id, "✅ " + message if success else "❌ " + message)

    def handle_init_admin_hwid_command(self, chat_id, parts):
        if len(parts) < 2:
            self.send_message(chat_id, "❌ Использование: /init_admin_hwid [ваш_HWID]")
            return
        
        admin_hwid = parts[1]
        users = self.users_db.get("users", [])
        
        if not users:
            # Если пользователей нет, регистрируем админа
            self.register_user(f"Admin_{chat_id}", admin_hwid)
            self.send_message(chat_id, f"✅ Админ-аккаунт инициализирован с HWID: {admin_hwid}. Теперь вы можете использовать другие команды.")
        else:
            self.send_message(chat_id, "❌ Пользователи уже существуют. Используйте /resethwid для изменения существующего HWID.")
        

    
    def handle_callback(self, callback_query):
        """Обработка callback от кнопок"""
        chat_id = callback_query["message"]["chat"]["id"]
        from_id = callback_query.get("from", {}).get("id")
        if not self.is_admin(chat_id, from_id):
            logger.info(f"Игнорирую callback от ID {from_id} — не админ")
            self.answer_callback(callback_query["id"])
            return
        callback_data = callback_query["data"]
        message_id = callback_query["message"]["message_id"]
        
        # Отвечаем на callback
        self.answer_callback(callback_query["id"])
        
        # Обработка нажатий
        if callback_data == "stats":
            self.show_stats(chat_id)
        elif callback_data == "users":
            self.show_users(chat_id)
        elif callback_data == "players":
            self.show_players(chat_id)
        elif callback_data == "online":
            self.show_online(chat_id)
        elif callback_data == "createkey_menu":
            self.show_createkey_menu(chat_id)
        elif callback_data == "manage_menu":
            self.show_manage_menu(chat_id)
        elif callback_data == "help":
            self.show_help(chat_id)
        elif callback_data == "back_to_main":
            self.send_admin_panel(chat_id)
        elif callback_data.startswith("createkey_"):
            plan = callback_data.replace("createkey_", "", 1)
            if plan in ("premium", "beta"):
                self.show_createkey_days(chat_id, plan)
            else:
                self.send_message(chat_id, "❌ Неизвестный тариф.")

        elif callback_data.startswith("key_days_"):
            parts = callback_data.replace("key_days_", "").split("_")
            if len(parts) != 2:
                self.send_message(chat_id, "❌ Некорректный срок ключа.")
            else:
                plan, days = parts
                if plan in ("premium", "beta") and days in ("7", "30", "90", "365"):
                    self.create_key_from_button(chat_id, plan, days)
                else:
                    self.send_message(chat_id, "❌ Некорректный тариф или срок.")

        elif callback_data == "manage_setplan":
            self.show_manage_setplan_input(chat_id)
        elif callback_data == "manage_ban":
            self.show_manage_ban_input(chat_id)
        elif callback_data == "manage_unban":
            self.show_manage_unban_input(chat_id)
    
    def answer_callback(self, callback_id, text=None):
        """Ответ на callback"""
        try:
            url = f"{self.api_url}/answerCallbackQuery"
            data = {"callback_query_id": callback_id}
            if text:
                data["text"] = text
            requests.post(url, json=data, timeout=10)
        except Exception as e:
            logger.error(f"Ошибка ответа на callback: {e}")
    
    def send_admin_panel(self, chat_id):
        """Отправка админ панели с кнопками"""
        message = """
🎛 <b>Админ панель Hynix</b>

Выберите действие:
        """
        
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📊 Статистика", "callback_data": "stats"},
                    {"text": "🎮 Игроки", "callback_data": "players"}
                ],
                [
                    {"text": "🟢 Онлайн", "callback_data": "online"},
                    {"text": "👥 База бота", "callback_data": "users"}
                ],
                [
                    {"text": "🔑 Создать ключ", "callback_data": "createkey_menu"}
                ],
                [
                    {"text": "👤 Управление", "callback_data": "manage_menu"},
                    {"text": "❓ Справка", "callback_data": "help"}
                ]
            ]
        }
        
        self.send_message(chat_id, message, reply_markup=keyboard)
    
    def show_createkey_menu(self, chat_id):
        """Меню создания ключа"""
        message = """
🔑 <b>Создание ключа</b>

Выберите план:
        """
        
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "💜 Premium", "callback_data": "createkey_premium"},
                    {"text": "🟡 Beta", "callback_data": "createkey_beta"}
                ],
                [
                    {"text": "◀️ Назад", "callback_data": "back_to_main"}
                ]
            ]
        }
        
        self.send_message(chat_id, message, reply_markup=keyboard)
    
    def show_createkey_days(self, chat_id, plan):
        """Выбор срока действия ключа"""
        plan_names = {"premium": "Premium", "beta": "Beta"}
        message = f"""
🔑 <b>Создание ключа: {plan_names[plan]}</b>

Выберите срок:
        """
        
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "7 дней", "callback_data": f"key_days_{plan}_7"},
                    {"text": "30 дней", "callback_data": f"key_days_{plan}_30"}
                ],
                [
                    {"text": "90 дней", "callback_data": f"key_days_{plan}_90"},
                    {"text": "365 дней", "callback_data": f"key_days_{plan}_365"}
                ],
                [
                    {"text": "◀️ Назад", "callback_data": "createkey_menu"}
                ]
            ]
        }
        
        self.send_message(chat_id, message, reply_markup=keyboard)
    
    def _local_key(self):
        import secrets
        import string
        return f"HYNIX-{''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))}-{secrets.token_hex(16)[:16].upper()}"

    def api_admin_post(self, path, payload):
        """POST к admin-эндпоинту Hynix API. Возвращает dict или None."""
        if not self.stats_api_base or not self.stats_admin_token:
            return None
        try:
            response = requests.post(
                f"{self.stats_api_base}{path}",
                headers={"Authorization": f"Bearer {self.stats_admin_token}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=15,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Ошибка API {path}: {e}")
            return None

    def api_create_key(self, plan, days):
        """Создать лицензионный ключ на Hynix API (рабочий для всех лоадеров)"""
        if not self.stats_api_base or not self.stats_admin_token:
            return None
        try:
            response = requests.post(
                f"{self.stats_api_base}/api/admin/keys",
                headers={"Authorization": f"Bearer {self.stats_admin_token}",
                         "Content-Type": "application/json"},
                json={"plan": plan, "durationDays": int(days)},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("key") if data.get("ok") else None
        except Exception as e:
            logger.error(f"Ошибка создания ключа через API: {e}")
            return None

    def create_key_from_button(self, chat_id, plan, days):
        """Создание ключа через кнопку"""
        key = self.api_create_key(plan, days)
        if not key:
            key = self._local_key()
            warn = "\n⚠️ API не настроен — это локальный ключ, в лоадере не сработает."
        else:
            warn = ""
        
        message = f"""
🔑 <b>Новый ключ создан!</b>

📦 <b>План:</b> {plan}
⏰ <b>Срок:</b> {days} дней
🔑 <b>Ключ:</b> <code>{key}</code>{warn}

Отправьте этот ключ пользователю для активации.
        """
        
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🔑 Создать ещё", "callback_data": "createkey_menu"},
                    {"text": "🏠 Меню", "callback_data": "back_to_main"}
                ]
            ]
        }
        
        self.send_message(chat_id, message, reply_markup=keyboard)
    
    def show_manage_menu(self, chat_id):
        """Меню управления пользователями"""
        message = """
👤 <b>Управление пользователями</b>

Выберите действие:
        """
        
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📦 Изменить план", "callback_data": "manage_setplan"},
                    {"text": "🚫 Заблокировать", "callback_data": "manage_ban"}
                ],
                [
                    {"text": "✅ Разблокировать", "callback_data": "manage_unban"},
                    {"text": "👥 Список юзеров", "callback_data": "users"}
                ],
                [
                    {"text": "◀️ Назад", "callback_data": "back_to_main"}
                ]
            ]
        }
        
        self.send_message(chat_id, message, reply_markup=keyboard)
    
    def show_manage_setplan_input(self, chat_id):
        """Ввод ID пользователя для изменения плана"""
        message = """
📦 <b>Изменение плана</b>

Введите <b>ник Minecraft</b> и <b>новый план</b>:

<code>/setplan [ник] [plan]</code>

Пример: <code>/setplan Steve premium</code>

Доступные планы: free, premium, beta
        """
        
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "◀️ Назад", "callback_data": "manage_menu"}
                ]
            ]
        }
        
        self.send_message(chat_id, message, reply_markup=keyboard)
    
    def show_manage_ban_input(self, chat_id):
        """Ввод ID пользователя для блокировки"""
        message = """
🚫 <b>Блокировка пользователя</b>

Введите <b>ник Minecraft</b> пользователя:

<code>/ban [ник]</code>

Пример: <code>/ban Steve</code>
        """
        
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "◀️ Назад", "callback_data": "manage_menu"}
                ]
            ]
        }
        
        self.send_message(chat_id, message, reply_markup=keyboard)
    
    def show_manage_unban_input(self, chat_id):
        """Ввод ID пользователя для разблокировки"""
        message = """
✅ <b>Разблокировка пользователя</b>

Введите <b>ник Minecraft</b> пользователя:

<code>/unban [ник]</code>

Пример: <code>/unban Steve</code>
        """
        
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "◀️ Назад", "callback_data": "manage_menu"}
                ]
            ]
        }
        
        self.send_message(chat_id, message, reply_markup=keyboard)
    
    def fetch_stats_users(self):
        """Получить реальную статистику лоадера с Hynix API"""
        if not self.stats_api_base or not self.stats_admin_token:
            return None
        try:
            response = requests.get(
                f"{self.stats_api_base}/api/admin/users",
                headers={"Authorization": f"Bearer {self.stats_admin_token}"},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("users", []) if isinstance(data, dict) else []
        except Exception as e:
            logger.error(f"Ошибка получения статистики API: {e}")
            return None

    @staticmethod
    def _fmt_play(seconds):
        total = max(0, int(seconds or 0))
        h, m = total // 3600, (total % 3600) // 60
        return f"{h}ч {m}м" if h else f"{m}м"

    @staticmethod
    def _days_left(expires_at):
        if not expires_at:
            return "∞"
        try:
            from datetime import datetime, timezone
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            diff = (expiry - datetime.now(timezone.utc)).total_seconds()
            if diff <= 0:
                return "истекла"
            return f"{int(diff // 86400) + 1} дн."
        except Exception:
            return "—"

    @staticmethod
    def _is_online(last_seen):
        if not last_seen:
            return False
        try:
            from datetime import datetime, timezone
            last = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - last).total_seconds() < 300
        except Exception:
            return False

    @staticmethod
    def _fmt_last_seen(last_seen):
        if not last_seen:
            return "никогда"
        try:
            from datetime import datetime, timezone
            last = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
            diff = (datetime.now(timezone.utc) - last).total_seconds()
            if diff < 60:
                return "только что"
            if diff < 3600:
                return f"{int(diff // 60)} мин назад"
            if diff < 86400:
                return f"{int(diff // 3600)} ч назад"
            return last.strftime("%d.%m.%Y")
        except Exception:
            return "—"

    def _stats_unavailable(self, chat_id):
        self.send_message(chat_id, "⚠️ Статистика недоступна.\n\nЗадай боту переменные окружения:\n<code>HYNIX_API_URL</code> и <code>HYNIX_ADMIN_TOKEN</code>")

    def show_players(self, chat_id):
        """Список реальных пользователей лоадера с сервера"""
        users = self.fetch_stats_users()
        if users is None:
            return self._stats_unavailable(chat_id)
        if not users:
            return self.send_message(chat_id, "📭 Пользователей пока нет — они появятся после входа в обновлённый лоадер.")

        users = sorted(users, key=lambda u: (u.get("uid") is None, u.get("uid") or 0))
        online = sum(1 for u in users if self._is_online(u.get("last_seen")))
        message = f"👥 <b>Пользователи лоадера</b> · онлайн: <b>{online}</b>\n\n"
        for u in users[:30]:
            dot = "🟢" if self._is_online(u.get("last_seen")) else "⚫"
            plan_emoji = {"premium": "💜", "beta": "🟡"}.get(u.get("plan"), "🆓")
            uid = f"#{u['uid']} " if u.get("uid") else ""
            message += (
                f"{dot} {uid}<b>{u.get('username', '?')}</b> {plan_emoji}{u.get('plan', 'free')}\n"
                f"   🎮 {u.get('version') or '—'} · ⏱ {self._fmt_play(u.get('total_play_seconds'))} · 🚀 {u.get('launches', 0)}\n"
                f"   📅 подписка: {self._days_left(u.get('expires_at'))} · 🕐 {self._fmt_last_seen(u.get('last_seen'))}\n"
                f"{'─' * 28}\n"
            )
        if len(users) > 30:
            message += f"…и ещё {len(users) - 30}"

        keyboard = {"inline_keyboard": [[{"text": "◀️ Назад", "callback_data": "back_to_main"}]]}
        self.send_message(chat_id, message, reply_markup=keyboard)

    def show_online(self, chat_id):
        """Кто сейчас в лоадере (активность за последние 5 минут)"""
        users = self.fetch_stats_users()
        if users is None:
            return self._stats_unavailable(chat_id)
        online = [u for u in users if self._is_online(u.get("last_seen"))]
        if not online:
            return self.send_message(chat_id, "😴 Сейчас в лоадере никого нет.")
        message = f"🟢 <b>Онлайн сейчас: {len(online)}</b>\n\n"
        for u in online:
            plan_emoji = {"premium": "💜", "beta": "🟡"}.get(u.get("plan"), "🆓")
            message += (
                f"• <b>{u.get('username', '?')}</b> {plan_emoji}{u.get('plan', 'free')} — "
                f"{u.get('version') or '—'} · ⏱ {self._fmt_play(u.get('total_play_seconds'))}\n"
            )
        keyboard = {"inline_keyboard": [[{"text": "◀️ Назад", "callback_data": "back_to_main"}]]}
        self.send_message(chat_id, message, reply_markup=keyboard)

    def show_users(self, chat_id):
        """Показать список пользователей"""
        users = self.users_db.get("users", [])
        
        if not users:
            self.send_message(chat_id, "📭 Пользователей нет")
            return
        
        message = "👥 <b>Список пользователей:</b>\n\n"
        
        for user in users:
            status = "✅" if user.get("status") != "banned" else "🚫"
            plan = user.get("plan", "free")
            created = user.get("createdAt", "unknown")
            
            message += f"""
{status} <b>ID:</b> {user.get('id')}
👤 <b>Имя:</b> {user.get('username')}
📦 <b>План:</b> {plan}
💻 <b>HWID:</b> {user.get('hwid', 'unknown')[:12]}...
📅 <b>Создан:</b> {created}
{'─' * 30}
"""
        
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "◀️ Назад", "callback_data": "back_to_main"}
                ]
            ]
        }
        
        self.send_message(chat_id, message, reply_markup=keyboard)
    
    def show_stats(self, chat_id):
        """Показать статистику"""
        users = self.users_db.get("users", [])
        
        total_users = len(users)
        active_users = len([u for u in users if u.get("status") != "banned"])
        banned_users = len([u for u in users if u.get("status") == "banned"])
        
        free_users = len([u for u in users if u.get("plan") == "free"])
        premium_users = len([u for u in users if u.get("plan") == "premium"])
        beta_users = len([u for u in users if u.get("plan") == "beta"])
        
        message = f"""
📊 <b>Статистика Hynix:</b>

👥 <b>Всего пользователей:</b> {total_users}
✅ <b>Активных:</b> {active_users}
🚫 <b>Заблокированных:</b> {banned_users}

📦 <b>Распределение планов:</b>
🆓 Free: {free_users}
💜 Premium: {premium_users}
🟡 Beta: {beta_users}
        """
        
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "◀️ Назад", "callback_data": "back_to_main"}
                ]
            ]
        }
        
        self.send_message(chat_id, message, reply_markup=keyboard)
    
    def create_key(self, chat_id, parts):
        """Создание ключа"""
        if len(parts) < 3:
            self.send_message(chat_id, "❌ Использование: /createkey [plan] [days]\n\nПример: /createkey premium 30")
            return
        
        plan = parts[1].lower()
        days = parts[2]
        
        if plan not in ["premium", "beta"]:
            self.send_message(chat_id, "❌ Неверный план. Доступно: premium, beta")
            return
        
        try:
            days_int = int(days)
            if days_int <= 0:
                raise ValueError
        except ValueError:
            self.send_message(chat_id, "❌ Должно быть положительное число дней")
            return
        
        # Генерация ключа через API (рабочий для всех лоадеров)
        key = self.api_create_key(plan, days_int)
        if not key:
            key = self._local_key()
            warn = "\n⚠️ API не настроен — это локальный ключ, в лоадере не сработает."
        else:
            warn = ""

        message = f"""
🔑 <b>Новый ключ создан!</b>

📦 <b>План:</b> {plan}
⏰ <b>Срок:</b> {days_int} дней
🔑 <b>Ключ:</b> <code>{key}</code>{warn}

Отправьте этот ключ пользователю для активации.
        """
        self.send_message(chat_id, message)
    
    def set_user_plan(self, chat_id, parts):
        """Изменение плана пользователя (сервер по нику, иначе локальная база по ID)"""
        if len(parts) < 3:
            self.send_message(chat_id, "❌ Использование: /setplan [ник или user_id] [plan]\n\nПример: /setplan Steve premium")
            return

        target = parts[1]
        plan = parts[2].lower()

        if plan not in ["free", "premium", "beta"]:
            self.send_message(chat_id, "❌ Неверный план. Доступно: free, premium, beta")
            return

        if self.stats_api_base and self.stats_admin_token:
            result = self.api_admin_post("/api/admin/users/setplan", {"username": target, "plan": plan})
            if result is None:
                self.send_message(chat_id, "❌ Сервер недоступен")
            elif result.get("updated"):
                self.send_message(chat_id, f"✅ План пользователя {target} изменён на {plan} (на сервере)")
            else:
                self.send_message(chat_id, f"❌ Пользователь «{target}» не найден на сервере")
            return

        users = self.users_db.get("users", [])
        user = next((u for u in users if str(u.get("id")) == target or str(u.get("username", "")).lower() == target.lower()), None)

        if not user:
            self.send_message(chat_id, f"❌ Пользователь «{target}» не найден")
            return

        user["plan"] = plan
        self.save_users_db()

        self.send_message(chat_id, f"✅ План пользователя {user.get('username')} (ID: {user.get('id')}) изменён на {plan}")

    def ban_user(self, chat_id, parts):
        """Блокировка пользователя (сервер по нику, иначе локальная база по ID)"""
        if len(parts) < 2:
            self.send_message(chat_id, "❌ Использование: /ban [ник или user_id]")
            return

        target = parts[1]

        if self.stats_api_base and self.stats_admin_token:
            result = self.api_admin_post("/api/admin/users/ban", {"username": target})
            if result is None:
                self.send_message(chat_id, "❌ Сервер недоступен")
            elif result.get("updated"):
                self.send_message(chat_id, f"🚫 Пользователь {target} заблокирован на сервере")
            else:
                self.send_message(chat_id, f"❌ Пользователь «{target}» не найден на сервере")
            return

        users = self.users_db.get("users", [])
        user = next((u for u in users if str(u.get("id")) == target or str(u.get("username", "")).lower() == target.lower()), None)

        if not user:
            self.send_message(chat_id, f"❌ Пользователь «{target}» не найден")
            return

        user["status"] = "banned"
        self.save_users_db()

        self.send_message(chat_id, f"🚫 Пользователь {user.get('username')} (ID: {user.get('id')}) заблокирован")

    def unban_user(self, chat_id, parts):
        """Разблокировка пользователя (сервер по нику, иначе локальная база по ID)"""
        if len(parts) < 2:
            self.send_message(chat_id, "❌ Использование: /unban [ник или user_id]")
            return

        target = parts[1]

        if self.stats_api_base and self.stats_admin_token:
            result = self.api_admin_post("/api/admin/users/unban", {"username": target})
            if result is None:
                self.send_message(chat_id, "❌ Сервер недоступен")
            elif result.get("updated"):
                self.send_message(chat_id, f"✅ Пользователь {target} разблокирован на сервере")
            else:
                self.send_message(chat_id, f"❌ Пользователь «{target}» не найден на сервере")
            return

        users = self.users_db.get("users", [])
        user = next((u for u in users if str(u.get("id")) == target or str(u.get("username", "")).lower() == target.lower()), None)

        if not user:
            self.send_message(chat_id, f"❌ Пользователь «{target}» не найден")
            return

        user["status"] = "active"
        self.save_users_db()

        self.send_message(chat_id, f"✅ Пользователь {user.get('username')} (ID: {user.get('id')}) разблокирован")

    def extend_user(self, chat_id, parts):
        """Продление подписки пользователя на сервере"""
        if len(parts) < 3:
            self.send_message(chat_id, "❌ Использование: /extend [ник] [дни]\n\nПример: /extend Steve 30")
            return

        target = parts[1]
        try:
            days = int(parts[2])
        except ValueError:
            self.send_message(chat_id, "❌ Дни должны быть числом")
            return

        if self.stats_api_base and self.stats_admin_token:
            result = self.api_admin_post("/api/admin/users/extend", {"username": target, "days": days})
            if result is None:
                self.send_message(chat_id, "❌ Сервер недоступен")
            elif result.get("updated"):
                self.send_message(chat_id, f"⏳ Подписка пользователя {target} продлена на {days} дн. (на сервере)")
            else:
                self.send_message(chat_id, f"❌ Пользователь «{target}» не найден на сервере")
            return

        self.send_message(chat_id, "❌ /extend работает только с сервером (задай HYNIX_API_URL и HYNIX_ADMIN_TOKEN)")

    def show_user(self, chat_id, parts):
        """Карточка пользователя с сервера"""
        if len(parts) < 2:
            self.send_message(chat_id, "❌ Использование: /user [ник]")
            return

        target = parts[1].lower()
        users = self.fetch_stats_users()
        if users is None:
            self._stats_unavailable(chat_id)
            return

        matches = [u for u in users if str(u.get("username", "")).lower() == target]
        if not matches:
            self.send_message(chat_id, f"❌ Пользователь «{parts[1]}» не найден на сервере")
            return

        lines = []
        for u in matches[:5]:
            online = "🟢" if self._is_online(u.get("last_seen")) else "⚪"
            banned = " 🚫ЗАБАНЕН" if u.get("banned") else ""
            locked = " 🔒" if u.get("plan_locked") else ""
            lines.append(
                f"{online} <b>{u.get('username')}</b>{banned}{locked}\n"
                f"   🆔 ID: {u.get('uid') or '—'} · Тариф: {u.get('plan', 'free')} · Подписка: {self._days_left(u.get('expires_at'))}\n"
                f"   Версия: {u.get('version') or '—'} · Клиент: {u.get('client_version') or '—'}\n"
                f"   В игре: {self._fmt_play(u.get('total_play_seconds'))} · Запусков: {u.get('launches', 0)}\n"
                f"   Был: {self._fmt_last_seen(u.get('last_seen'))}"
            )
        self.send_message(chat_id, "\n\n".join(lines))

    def reset_hwid(self, user_id):
        """Сброс HWID пользователя"""
        users = self.users_db.get("users", [])
        user = next((u for u in users if str(u.get("id")) == user_id), None)
        
        if not user:
            return False, f"Пользователь с ID {user_id} не найден"

        user["hwid"] = ""
        self.save_users_db()
        return True, f"HWID пользователя {user.get('username')} (ID: {user_id}) сброшен."
    
    def show_help(self, chat_id):
        """Показ справки"""
        message = """
        📖 <b>Справка по командам:</b>

📊 <b>Статистика:</b>
/stats - Статистика базы бота
/players - Реальные пользователи лоадера (с сервера)
/online - Кто сейчас онлайн в лоадере
/users - Локальная база бота

🔑 <b>Ключи:</b>
/createkey [plan] [days] - Создать ключ

👤 <b>Управление (ник Minecraft):</b>
/players - Все пользователи сервера
/online - Кто онлайн
/user [ник] - Карточка пользователя
/setplan [ник] [plan] - Изменить план (free/premium/beta)
/extend [ник] [дни] - Продлить подписку
/ban [ник] - Заблокировать
/unban [ник] - Разблокировать
/resethwid [user_id] - Сбросить HWID (локальная база)
/init_admin_hwid [hwid] - Инициализировать админ HWID (использовать только если нет аккаунтов)

❓ /help - Эта справка
        """
        
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "◀️ Назад", "callback_data": "back_to_main"}
                ]
            ]
        }
        
        self.send_message(chat_id, message, reply_markup=keyboard)
    
    def update_user_data(self, user_id, username, hwid, plan):
        """Обновление данных пользователя (вызывается из лоадера)"""
        users = self.users_db.get("users", [])
        user = next((u for u in users if u.get("id") == user_id), None)
        
        if user:
            user["username"] = username
            user["hwid"] = hwid
            if plan:
                user["plan"] = plan
            user["lastSeen"] = datetime.now().isoformat()
        else:
            new_user = {
                "id": user_id,
                "username": username,
                "hwid": hwid,
                "plan": plan or "free",
                "status": "active",
                "createdAt": datetime.now().isoformat(),
                "lastSeen": datetime.now().isoformat()
            }
            users.append(new_user)
        
        self.users_db["users"] = users
        self.save_users_db()
    
    def register_user(self, username, hwid):
        """Регистрация нового пользователя"""
        users = self.users_db.get("users", [])
        
        # Проверка существующего HWID
        existing = next((u for u in users if u.get("hwid") == hwid), None)
        if existing:
            return existing.get("id"), existing.get("plan", "free")
        
        # Создание нового пользователя
        user_id = len(users) + 1
        new_user = {
            "id": user_id,
            "username": username,
            "hwid": hwid,
            "plan": "free",
            "status": "active",
            "createdAt": datetime.now().isoformat(),
            "lastSeen": datetime.now().isoformat()
        }
        users.append(new_user)
        self.users_db["users"] = users
        self.save_users_db()
        
        self.send_message(self.admin_chat_id, f"""
👤 <b>Новый пользователь!</b>

🆔 <b>ID:</b> {user_id}
👤 <b>Имя:</b> {username}
💻 <b>HWID:</b> {hwid[:12]}...
📦 <b>План:</b> Free
⏰ <b>Время:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        """)
        
        return user_id, "free"
    
    def send_launch_notification(self, user_id, username, version, hwid):
        """Уведомление о запуске"""
        self.update_user_data(user_id, username, hwid, None)
        
        message = f"""
🚀 <b>Запуск Hynix!</b>

🆔 <b>ID:</b> {user_id}
👤 <b>Имя:</b> {username}
🎮 <b>Версия:</b> {version}
💻 <b>HWID:</b> {hwid[:12]}...
⏰ <b>Время:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        """
        return self.send_message(self.admin_chat_id, message)
    
    def send_key_activation(self, user_id, username, plan, hwid):
        """Уведомление об активации ключа"""
        self.update_user_data(user_id, username, hwid, plan)
        
        message = f"""
🔑 <b>Ключ активирован!</b>

🆔 <b>ID:</b> {user_id}
👤 <b>Имя:</b> {username}
📦 <b>Новый план:</b> {plan}
💻 <b>HWID:</b> {hwid[:12]}...
⏰ <b>Время:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        """
        return self.send_message(self.admin_chat_id, message)

def create_bot_from_environment():
    """Create the bot only when credentials are provided through the environment."""
    bot_token = os.environ.get("BOT_TOKEN") or os.environ.get("HYNIX_TELEGRAM_BOT_TOKEN")
    admin_chat_id = os.environ.get("ADMIN_CHAT_ID") or os.environ.get("HYNIX_TELEGRAM_CHAT_ID")
    if not bot_token or not admin_chat_id:
        raise RuntimeError("Set BOT_TOKEN and ADMIN_CHAT_ID environment variables first.")
    return HynixTelegramBot(bot_token, admin_chat_id)


if __name__ == "__main__":
    import sys

    try:
        bot = create_bot_from_environment()
    except RuntimeError as error:
        raise SystemExit(str(error)) from error

    if len(sys.argv) <= 1:
        print("Use bot_runner.py to start polling, or pass a supported notification method.")
        raise SystemExit(0)

    method, *args = sys.argv[1:]
    try:
        if method == "register_user":
            username, hwid = args
            user_id, plan = bot.register_user(username, hwid)
            print(json.dumps({"success": True, "user_id": user_id, "plan": plan}))
        elif method == "send_launch_notification":
            user_id, username, version, hwid = args
            result = bot.send_launch_notification(int(user_id), username, version, hwid)
            print(json.dumps({"success": result}))
        elif method == "send_key_activation":
            user_id, username, plan, hwid = args
            result = bot.send_key_activation(int(user_id), username, plan, hwid)
            print(json.dumps({"success": result}))
        else:
            print(json.dumps({"success": False, "error": "Unknown method"}))
            raise SystemExit(2)
    except Exception as error:
        print(json.dumps({"success": False, "error": str(error)}))
        raise SystemExit(1)
