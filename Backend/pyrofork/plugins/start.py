from datetime import datetime

from pyrogram import Client, enums, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from Backend import db
from Backend.config import Telegram
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER

def _currency_symbol(code):
    return {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "AUD": "A$", "CAD": "C$", "SGD": "S$", "AED": "د.إ", "BRL": "R$"}.get((code or "INR").upper(), f"{(code or 'INR')} ")



#----- /start: hand out the Stremio addon link, gated by subscription state
@Client.on_message(filters.command('start') & filters.private, group=10)
async def send_start_message(client: Client, message: Message):
    try:
        user_id = (message.from_user.id if message.from_user else None) or (message.sender_chat.id if message.sender_chat else None) or message.chat.id
        base_url = SettingsManager.current().base_url
        addon_url = f"{base_url}/stremio/manifest.json"

        #----- No subscription mode: owner-only, single personal token
        if not SettingsManager.current().subscription:
            if user_id != Telegram.OWNER_ID:
                return
            user_name = (message.from_user.first_name or message.from_user.username or f"User {user_id}") if message.from_user else f"Chat {user_id}"
            try:
                token_doc = await db.add_api_token(name=user_name, user_id=user_id)
                token_token = token_doc.get('token') if isinstance(token_doc, dict) else None
                addon_url = f"{base_url}/stremio/{token_token}/manifest.json" if token_token else f"{base_url}/stremio/manifest.json"
                nuvio_url = f"{base_url}/stremio/{token_token}/nuvio-collection.json" if token_token else f"{base_url}/stremio/nuvio-collection.json"
                nuvio_ott_url = f"{base_url}/stremio/{token_token}/nuvio-ott-collection.json" if token_token else f"{base_url}/stremio/nuvio-ott-collection.json"
                nuvio_explore_url = f"{base_url}/stremio/{token_token}/nuvio-explore-collection.json" if token_token else f"{base_url}/stremio/nuvio-explore-collection.json"
            except Exception as e:
                LOGGER.error(f"Error ensuring token for free user: {e}")
                nuvio_url = f"{base_url}/stremio/nuvio-collection.json"
                nuvio_ott_url = f"{base_url}/stremio/nuvio-ott-collection.json"
                nuvio_explore_url = f"{base_url}/stremio/nuvio-explore-collection.json"

            await message.reply_text(
                '🎉 <b>Welcome to the Telegram Stremio Media Server!</b>\n\n'
                'Here are your personal links:\n\n'
                '🧩 <b>Addon Manifest URL (Stremio & Nuvio):</b>\n'
                f'<code>{addon_url}</code>\n\n'
                '📱 <b>Nuvio Collection URLs (Optional):</b>\n'
                f'• <a href="{nuvio_url}">Full Collection</a>\n'
                f'• <a href="{nuvio_ott_url}">OTT Platforms Only</a>\n'
                f'• <a href="{nuvio_explore_url}">TMDb Franchises Only</a>\n\n'
                '• Add the <b>Addon Manifest URL</b> into Stremio or Nuvio under Addons.\n'
                '• Use the <b>Nuvio Collection File</b> if you want logo catalog buttons on Nuvio home screen!',
                quote=True,
                parse_mode=enums.ParseMode.HTML
            )

            if token_token:
                try:
                    import io, json
                    from Backend.fastapi.routes.stremio_routes import build_nuvio_collection_data
                    for mode, fname, label in [
                        ("full", "nuvio_collection.json", "Full Collection (OTT + Franchises)"),
                        ("ott", "nuvio_ott_collection.json", "OTT Platforms Only"),
                        ("explore", "nuvio_explore_collection.json", "TMDb & Trakt Franchises Only"),
                    ]:
                        col_data = await build_nuvio_collection_data(token_token, mode)
                        json_bytes = json.dumps(col_data, indent=2, ensure_ascii=False).encode("utf-8")
                        file_obj = io.BytesIO(json_bytes)
                        file_obj.name = fname
                        await client.send_document(
                            chat_id=message.chat.id,
                            document=file_obj,
                            caption=f"📱 <b>Nuvio {label} File</b>\n\nDownload this file and import directly into Nuvio!"
                        )
                except Exception as e:
                    LOGGER.error(f"Error sending Nuvio collection files in free start: {e}")
            return

        #----- Subscription mode: verify active subscription, else offer plans
        user = await db.get_user(user_id)
        now = datetime.utcnow()

        is_active = db.is_subscription_active(user, now)
        if not is_active and user and user.get("subscription_status") == "active":
            await db.mark_user_expired(user_id)

        #----- Honour a manual token grant (never-expires or a future token expiry)
        if not is_active:
            token_doc = await db.get_api_token_by_user(user_id)
            if token_doc and (token_doc.get("subscription_exempt")
                              or (token_doc.get("expires_at") and token_doc["expires_at"] > now)):
                is_active = True

        if not is_active:
            plans = await db.get_subscription_plans()
            if not plans:
                return await message.reply_text(
                    '<b>Welcome to the Telegram Stremio Private Group!</b>\n\n'
                    'Currently, no subscription plans are set up. Please contact the administrator.',
                    quote=True,
                    parse_mode=enums.ParseMode.HTML
                )

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{plan['days']} Days - {_currency_symbol(plan.get('currency'))}{plan['price']}", callback_data=f"plan_{plan['_id']}")]
                for plan in plans
            ])
            return await message.reply_text(
                '<b>Welcome to the Telegram Stremio Private Group!</b>\n\n'
                'Access to this bot and the Stremio Addon requires an active subscription.\n'
                'Please select a subscription plan below to continue:',
                reply_markup=keyboard,
                quote=True,
                parse_mode=enums.ParseMode.HTML
            )

        #----- Active subscriber: return their token link, creating one if missing
        user_name = (user.get("first_name") or user.get("username")) if user else None
        token_doc = await db.ensure_api_token_for_user(user_id, user_name)
        if token_doc and token_doc.get("token"):
            addon_url = f"{base_url}/stremio/{token_doc['token']}/manifest.json"
            nuvio_url = f"{base_url}/stremio/{token_doc['token']}/nuvio-collection.json"
            nuvio_ott_url = f"{base_url}/stremio/{token_doc['token']}/nuvio-ott-collection.json"
            nuvio_explore_url = f"{base_url}/stremio/{token_doc['token']}/nuvio-explore-collection.json"
        else:
            nuvio_url = f"{base_url}/stremio/nuvio-collection.json"
            nuvio_ott_url = f"{base_url}/stremio/nuvio-ott-collection.json"
            nuvio_explore_url = f"{base_url}/stremio/nuvio-explore-collection.json"

        await message.reply_text(
            '🎉 <b>Welcome back to the Telegram Stremio Subscription Manager!</b>\n\n'
            'Your subscription is active. Here are your personal links:\n\n'
            '🧩 <b>Addon Manifest URL (Stremio & Nuvio):</b>\n'
            f'<code>{addon_url}</code>\n\n'
            '📱 <b>Nuvio Collection URLs (Optional):</b>\n'
            f'• <a href="{nuvio_url}">Full Collection</a>\n'
            f'• <a href="{nuvio_ott_url}">OTT Platforms Only</a>\n'
            f'• <a href="{nuvio_explore_url}">TMDb Franchises Only</a>\n\n'
            '• Add the <b>Addon Manifest URL</b> into Stremio or Nuvio under Addons.\n'
            '• Use the <b>Nuvio Collection File</b> if you want logo catalog buttons on Nuvio home screen!',
            quote=True,
            parse_mode=enums.ParseMode.HTML
        )

        if token_doc and token_doc.get("token"):
            try:
                import io, json
                from Backend.fastapi.routes.stremio_routes import build_nuvio_collection_data
                for mode, fname, label in [
                    ("full", "nuvio_collection.json", "Full Collection (OTT + Franchises)"),
                    ("ott", "nuvio_ott_collection.json", "OTT Platforms Only"),
                    ("explore", "nuvio_explore_collection.json", "TMDb & Trakt Franchises Only"),
                ]:
                    col_data = await build_nuvio_collection_data(token_doc.get("token"), mode)
                    json_bytes = json.dumps(col_data, indent=2, ensure_ascii=False).encode("utf-8")
                    file_obj = io.BytesIO(json_bytes)
                    file_obj.name = fname
                    await client.send_document(
                        chat_id=message.chat.id,
                        document=file_obj,
                        caption=f"📱 <b>Nuvio {label} File</b>\n\nDownload this file and import directly into Nuvio!"
                    )
            except Exception as e:
                LOGGER.error(f"Error sending Nuvio collection file in start: {e}")

    except Exception as e:
        await message.reply_text(f"⚠️ Error: {e}")
        LOGGER.error(f"Error in /start handler: {e}")
