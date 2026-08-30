import os
import re
import aiohttp
import base64
import json
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Конфигурация
TOKEN = os.getenv('TELEGRAM_TOKEN')
OSU_CLIENT_ID = os.getenv('OSU_CLIENT_ID')
OSU_CLIENT_SECRET = os.getenv('OSU_CLIENT_SECRET')
OSU_API_URL = 'https://osu.ppy.sh/api/v2'
OSU_TOKEN_URL = 'https://osu.ppy.sh/oauth/token'

def get_beatmap_data(score_data):
    """Извлекает данные карты из скора, пробуя разные варианты"""
    # Пробуем получить beatmap
    beatmap = score_data.get('beatmap', {})
    
    # Пробуем разные поля для artist
    artist = None
    title = None
    version = None
    
    # 1. Из beatmap
    if beatmap:
        artist = beatmap.get('artist')
        title = beatmap.get('title')
        version = beatmap.get('version')
    
    # 2. Из beatmapset (если есть)
    if not artist or not title:
        beatmapset = score_data.get('beatmapset', {})
        if beatmapset:
            if not artist:
                artist = beatmapset.get('artist')
            if not title:
                title = beatmapset.get('title')
    
    # 3. Из самого скора
    if not artist:
        artist = score_data.get('artist')
    if not title:
        title = score_data.get('title')
    if not version:
        version = score_data.get('version')
    
    # 4. Из вложенных полей в скоре
    if not artist or not title:
        for key, value in score_data.items():
            if isinstance(value, dict):
                if 'artist' in value and not artist:
                    artist = value.get('artist')
                if 'title' in value and not title:
                    title = value.get('title')
                if 'version' in value and not version:
                    version = value.get('version')
    
    # 5. Если все еще нет, используем значения по умолчанию
    if not artist:
        artist = 'Unknown Artist'
    if not title:
        title = 'Unknown Title'
    if not version:
        version = 'Unknown'
    
    return artist, title, version

class OsuApiV2:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = None
        self.token_expires_at = None
        self.session = None

    async def get_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self.session

    async def get_access_token(self):
        if self.access_token and self.token_expires_at and datetime.now() < self.token_expires_at:
            return self.access_token

        session = await self.get_session()
        
        auth_string = f"{self.client_id}:{self.client_secret}"
        auth_bytes = auth_string.encode('utf-8')
        auth_base64 = base64.b64encode(auth_bytes).decode('utf-8')

        headers = {
            'Authorization': f'Basic {auth_base64}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }

        data = {
            'grant_type': 'client_credentials',
            'scope': 'public'
        }

        try:
            async with session.post(OSU_TOKEN_URL, headers=headers, data=data) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"Ошибка получения токена: {error_text}")
                
                token_data = await resp.json()
                self.access_token = token_data['access_token']
                self.token_expires_at = datetime.now() + timedelta(seconds=token_data.get('expires_in', 86400))
                return self.access_token
        except Exception as e:
            raise Exception(f"Ошибка авторизации: {str(e)}")

    async def make_request(self, endpoint, params=None):
        token = await self.get_access_token()
        session = await self.get_session()

        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }

        url = f"{OSU_API_URL}/{endpoint}"
        
        try:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 401:
                    self.access_token = None
                    token = await self.get_access_token()
                    headers['Authorization'] = f'Bearer {token}'
                    async with session.get(url, headers=headers, params=params) as retry_resp:
                        return await retry_resp.json()
                
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"Ошибка API: {error_text}")
                
                return await resp.json()
        except Exception as e:
            raise Exception(f"Ошибка запроса: {str(e)}")

    async def get_user(self, username):
        try:
            data = await self.make_request(f'users/{username}/osu')
            
            if not data:
                return None

            return {
                'username': data.get('username', 'N/A'),
                'country': data.get('country', {}).get('code', 'N/A'),
                'pp_rank': data.get('statistics', {}).get('global_rank'),
                'pp_country_rank': data.get('statistics', {}).get('country_rank'),
                'pp_raw': data.get('statistics', {}).get('pp'),
                'accuracy': data.get('statistics', {}).get('hit_accuracy', 0),
                'playcount': data.get('statistics', {}).get('play_count', 0),
                'playtime': data.get('statistics', {}).get('play_time', 0),
                'level': data.get('statistics', {}).get('level', {}).get('current', 0),
                'followers': data.get('follower_count', 0),
                'user_id': data.get('id')
            }
        except Exception as e:
            print(f"Ошибка получения пользователя {username}: {e}")
            return None

    async def get_user_best(self, username, limit=10):
        try:
            user_data = await self.get_user(username)
            if not user_data:
                return None
            
            user_id = user_data['user_id']
            
            scores = await self.make_request(
                f'users/{user_id}/scores/best',
                params={'limit': limit}
            )
            
            return scores
        except Exception as e:
            print(f"Ошибка получения топ-скоров: {e}")
            return None

    async def get_user_recent(self, username):
        try:
            user_data = await self.get_user(username)
            if not user_data:
                return None
            
            user_id = user_data['user_id']
            
            scores = await self.make_request(
                f'users/{user_id}/scores/recent',
                params={'limit': 1}
            )
            
            return scores[0] if scores else None
        except Exception as e:
            print(f"Ошибка получения последнего скора: {e}")
            return None

    def format_number(self, num):
        if num is None:
            return 'N/A'
        if isinstance(num, (int, float)):
            return f'{int(num):,}'.replace(',', ',')
        return str(num)

    def format_rank(self, rank):
        if rank is None:
            return 'N/A'
        return f'#{int(rank):,}'.replace(',', ',')

    def format_pp(self, pp):
        if pp is None:
            return 'N/A'
        return f'{int(pp):,}'.replace(',', ',')

# Создаем экземпляр API
osu_api = OsuApiV2(OSU_CLIENT_ID, OSU_CLIENT_SECRET)

# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "*Добро пожаловать в osu! Profile Bot!*\n\n"
        "Я помогу вам получить информацию о профилях игроков в osu!\n\n"
        "*Доступные команды:*\n"
        "/profile `<username>` - Показать профиль игрока\n"
        "/top `<username>` - Показать Top 10 карт\n"
        "/recent `<username>` - Показать последний скор\n"
        "/compare `<user1> <user2>` - Сравнить двух игроков\n"
    )
    msg = await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        msg = await update.message.reply_text(
            "❌ Пожалуйста, укажите имя пользователя.\nПример: `/profile WhiteCat`",
            parse_mode='Markdown'
        )
        return

    username = ' '.join(context.args)
    msg = await update.message.reply_text(
        f"🔍 Ищу профиль для *{(username)}*...",
        parse_mode='Markdown'
    )
    loading_msg = msg

    try:
        user_data = await osu_api.get_user(username)
        
        if not user_data:
            await loading_msg.edit_text(
                f"❌ Пользователь *{(username)}* не найден!",
                parse_mode='Markdown'
            )
            return

        profile_text = (
            f"👤 *{(user_data.get('username', 'N/A'))}*\n\n"
            f"🌍 {(user_data.get('country', 'N/A'))}\n"
            f"🏆 Ранг: {osu_api.format_rank(user_data.get('pp_rank'))}\n"
            f" Ранг в стране: {osu_api.format_rank(user_data.get('pp_country_rank'))}\n\n"
            f"⚡ PP: {osu_api.format_pp(user_data.get('pp_raw'))}\n"
            f"🎯 Точность: {user_data.get('accuracy', 0):.2f}%\n\n"
            f"🎮 Количество игр: {osu_api.format_number(user_data.get('playcount'))}\n"
            f"⏱️ Время игры: {int(user_data.get('playtime', 0)) // 3600}ч\n"
            f"📊 Уровень: {user_data.get('level', 0):.2f}\n"
            f"👥 Подписчики: {osu_api.format_number(user_data.get('followers'))}"
        )

        await loading_msg.edit_text(profile_text, parse_mode='Markdown')

    except Exception as e:
        await loading_msg.edit_text(
            f"❌ Произошла ошибка: {str(e)}",
            parse_mode='Markdown'
        )

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        msg = await update.message.reply_text(
            "❌ Пожалуйста, укажите имя пользователя.\nПример: `/top WhiteCat`",
            parse_mode='Markdown'
        )
        return

    username = ' '.join(context.args)
    msg = await update.message.reply_text(
        f"🔍 Ищу топ карты для *{username}*...",
        parse_mode='Markdown'
    )
    loading_msg = msg

    try:
        best_scores = await osu_api.get_user_best(username)
        
        if not best_scores:
            await loading_msg.edit_text(
                f"❌ У пользователя *{username}* нет лучших результатов!",
                parse_mode='Markdown'
            )
            return

        response = f"🎵 *Топ 10 карт для {(username)}*\n\n"
        
        for idx, score in enumerate(best_scores[:10], 1):
            try:
                # Используем универсальную функцию для получения данных
                artist, title, version = get_beatmap_data(score)
                
                # Формируем название
                song_name = f"{artist} - {title} [{version}]"
                # Экранируем только открывающую скобку
                song_name = song_name.replace('[', '\\[')
                
                pp = score.get('pp', 0)
                if isinstance(pp, str):
                    pp = float(pp) if pp else 0
                
                accuracy = score.get('accuracy', 0) * 100
                combo = score.get('max_combo', 0)
                mods = score.get('mods', [])
                mods_str = f" +{','.join(mods)}" if mods else ""
                
                response += (
                    f"*{idx}.* {song_name}{mods_str}\n"
                    f"   PP: {pp:.0f} | Точность: {accuracy:.2f}% | Комбо: {combo}\n\n"
                )
                    
            except Exception as e:
                print(f"Ошибка при обработке скора {idx}: {e}")
                response += f"*{idx}.* Ошибка загрузки\n\n"

        if len(response) > 4096:
            await loading_msg.edit_text("📊 Результатов слишком много, попробуйте использовать более короткое имя.")
        else:
            await loading_msg.edit_text(response, parse_mode='Markdown')

    except Exception as e:
        await loading_msg.edit_text(
            f"❌ Произошла ошибка: {str(e)}",
            parse_mode='Markdown'
        )

async def recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        msg = await update.message.reply_text(
            "❌ Пожалуйста, укажите имя пользователя.\nПример: `/recent WhiteCat`",
            parse_mode='Markdown'
        )
        return

    username = ' '.join(context.args)
    msg = await update.message.reply_text(
        f"🔍 Ищу последний скор для *{username}*...",
        parse_mode='Markdown'
    )
    loading_msg = msg

    try:
        recent_score = await osu_api.get_user_recent(username)
        
        if not recent_score:
            await loading_msg.edit_text(
                f"❌ У пользователя *{username}* нет недавних скоров!",
                parse_mode='Markdown'
            )
            return

        # Используем универсальную функцию для получения данных
        artist, title, version = get_beatmap_data(recent_score)
        
        # Формируем название
        song_name = f"{artist} - {title} [{version}]"
        # Экранируем только открывающую скобку
        song_name = song_name.replace('[', '\\[')
        
        accuracy = recent_score.get('accuracy', 0) * 100
        combo = recent_score.get('max_combo', 0)
        score_val = recent_score.get('score', 0)
        mods = recent_score.get('mods', [])
        mods_str = f" +{','.join(mods)}" if mods else ""
        
        is_new_record = " 🎉 **НОВЫЙ РЕКОРД!**" if recent_score.get('is_perfect_combo', False) or recent_score.get('rank') in ['SH', 'XH', 'X'] else ""

        recent_text = (
            f"🎵 *Последний скор {username}*{is_new_record}\n\n"
            f"📝 {song_name}{mods_str}\n"
            f"🎯 Точность: {accuracy:.2f}%\n"
            f"💯 Счет: {score_val:,}\n"
            f"🔗 Комбо: {combo}\n"
            f"🏅 Ранг: {recent_score.get('rank', 'N/A')}"
        )

        await loading_msg.edit_text(recent_text, parse_mode='Markdown')

    except Exception as e:
        await loading_msg.edit_text(
            f"❌ Произошла ошибка: {str(e)}",
            parse_mode='Markdown'
        )

async def compare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        msg = await update.message.reply_text(
            "❌ Пожалуйста, укажите двух пользователей.\n"
            "Пример: `/compare WhiteCat mrekk`",
            parse_mode='Markdown'
        )
        return

    user1 = context.args[0]
    user2 = context.args[1]
    
    msg = await update.message.reply_text(
        f"🔍 Сравниваю *{user1}* и *{user2}*...",
        parse_mode='Markdown'
    )
    loading_msg = msg

    try:
        user1_data = await osu_api.get_user(user1)
        user2_data = await osu_api.get_user(user2)

        if not user1_data or not user2_data:
            missing = []
            if not user1_data: missing.append(user1)
            if not user2_data: missing.append(user2)
            await loading_msg.edit_text(
                f"❌ Пользователи не найдены: {', '.join(missing)}"
            )
            return

        def get_compare_emoji(val1, val2):
            if val1 is None or val2 is None:
                return "➖"
            if isinstance(val1, int) and isinstance(val2, int):
                if val1 < val2:
                    return "✅"
                elif val1 > val2:
                    return "❌"
            if isinstance(val1, (int, float)) and isinstance(val2, (int, float)):
                if val1 > val2:
                    return "✅"
                elif val1 < val2:
                    return "❌"
            return "➖"

        compare_text = (
            f"⚔️ *Сравнение игроков*\n\n"
            f"*{user1_data['username']}* vs *{user2_data['username']}*\n\n"
            f"🏆 Ранг:\n"
            f"  {user1_data['username']}: {osu_api.format_rank(user1_data.get('pp_rank'))} {get_compare_emoji(user1_data.get('pp_rank'), user2_data.get('pp_rank'))}\n"
            f"  {user2_data['username']}: {osu_api.format_rank(user2_data.get('pp_rank'))}\n\n"
            f"⚡ PP:\n"
            f"  {user1_data['username']}: {osu_api.format_pp(user1_data.get('pp_raw'))} {get_compare_emoji(user1_data.get('pp_raw'), user2_data.get('pp_raw'))}\n"
            f"  {user2_data['username']}: {osu_api.format_pp(user2_data.get('pp_raw'))}\n\n"
            f"🎯 Точность:\n"
            f"  {user1_data['username']}: {user1_data.get('accuracy', 0):.2f}% {get_compare_emoji(user1_data.get('accuracy'), user2_data.get('accuracy'))}\n"
            f"  {user2_data['username']}: {user2_data.get('accuracy', 0):.2f}%\n\n"
            f"🎮 Количество игр:\n"
            f"  {user1_data['username']}: {osu_api.format_number(user1_data.get('playcount'))} {get_compare_emoji(user1_data.get('playcount'), user2_data.get('playcount'))}\n"
            f"  {user2_data['username']}: {osu_api.format_number(user2_data.get('playcount'))}"
        )

        await loading_msg.edit_text(compare_text, parse_mode='Markdown')

    except Exception as e:
        await loading_msg.edit_text(
            f"❌ Произошла ошибка: {str(e)}",
            parse_mode='Markdown'
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "*📖 Помощь по командам*\n\n"
        "/start - Показать приветственное сообщение\n"
        "/help - Показать эту справку\n"
        "*👤 Команды профиля:*\n"
        "/profile `<username>` - Показать профиль игрока\n"
        "/top `<username>` - Показать топ 10 карт\n"
        "/recent `<username>` - Показать последний скор\n"
        "/compare `<user1> <user2>` - Сравнить двух игроков\n\n"
    )
    
    msg = await update.message.reply_text(help_text, parse_mode='Markdown')


def main():
    """Главная функция"""
    if not TOKEN:
        print("❌ Ошибка: TELEGRAM_TOKEN не найден в .env файле")
        return

    if not OSU_CLIENT_ID or not OSU_CLIENT_SECRET:
        print("❌ Ошибка: OSU_CLIENT_ID или OSU_CLIENT_SECRET не найдены в .env файле")
        return

    print("🚀 Бот запущен!")
    
    # Создаем приложение
    application = Application.builder().token(TOKEN).build()

    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CommandHandler("top", top))
    application.add_handler(CommandHandler("recent", recent))
    application.add_handler(CommandHandler("compare", compare))

    # Запускаем бота
    application.run_polling()

if __name__ == '__main__':
    main()