# coding=utf-8

import html
import logging
import mimetypes
import os
from gettext import NullTranslations, translation
from typing import Optional
from xmlrpc.server import SimpleXMLRPCServer

import telegram
import telegram.constants
import telegram.error
import telegram.ext
import yaml
from PIL import Image
from pkg_resources import resource_filename

from ehforwarderbot import EFBChannel, EFBMsg, EFBStatus, coordinator
from ehforwarderbot import utils as efb_utils
from ehforwarderbot.constants import MsgType, ChannelType
from ehforwarderbot.exceptions import EFBException
from . import __version__ as version
from . import utils as etm_utils
from .bot_manager import TelegramBotManager
from .chat_binding import ChatBindingManager, ETMChat
from .commands import CommandsManager
from .db import DatabaseManager
from .global_command_handler import GlobalCommandHandler
from .master_message import MasterMessageProcessor
from .rpc_utils import RPCUtilities
from .slave_message import SlaveMessageProcessor
from .utils import ExperimentalFlagsManager
from .voice_recognition import VoiceRecognitionManager


class TelegramChannel(EFBChannel):
    """
    EFB Channel - Telegram (Master)
    Based on python-telegram-bot, Telegram Bot API

    Author: Eana Hufwe <https://github.com/blueset>

    External Services:
        You may need API keys from following service providers to use speech recognition.
        Bing Speech API: https://www.microsoft.com/cognitive-services/en-us/speech-api
        Baidu Speech Recognition API: http://yuyin.baidu.com/

    Configuration file example:
        .. code-block:: yaml
            
            token: "12345678:1a2b3c4d5e6g7h8i9j"
            admins:
            - 102938475
            - 91827364
            speech_api:
                bing: "VOICE_RECOGNITION_TOKEN"
                baidu:
                    app_id: 123456
                    api_key: "API_KEY_GOES_HERE"
                    secret_key: "SECRET_KEY_GOES_HERE"
            flags:
                join_msg_threshold_secs: 10
                multiple_slave_chats: false
    """

    # Meta Info
    channel_name = "Telegram Master"
    channel_emoji = "✈"
    channel_id = "blueset.telegram"
    channel_type = ChannelType.Master
    supported_message_types = {MsgType.Text, MsgType.File, MsgType.Audio,
                               MsgType.Image, MsgType.Link, MsgType.Location,
                               MsgType.Sticker, MsgType.Video}
    __version__ = version.__version__

    # Data
    msg_status = {}
    msg_storage = {}
    _stop_polling = False
    timeout_count = 0

    # Constants
    config = None

    # Slave-only channels
    get_chat = None
    get_chats = None
    get_chat_picture = None

    # Translator
    translator: NullTranslations = translation("efb_telegram_master",
                                               resource_filename('efb_telegram_master', 'locale'),
                                               fallback=True)
    locale: str = None

    # RPC server
    rpc_server: SimpleXMLRPCServer = None

    def __init__(self, instance_id: str = None):
        """
        Initialization.
        """
        super().__init__(instance_id)

        # Check PIL support for WebP
        Image.init()
        if 'WEBP' not in Image.ID:
            raise EFBException(self._("WebP support of Pillow is required.\n"
                                      "Please refer to Pillow Documentation for instructions.\n"
                                      "https://pillow.readthedocs.io/"))

        # Suppress debug logs from dependencies
        logging.getLogger('requests').setLevel(logging.CRITICAL)
        logging.getLogger('urllib3').setLevel(logging.CRITICAL)
        logging.getLogger('telegram.bot').setLevel(logging.CRITICAL)
        logging.getLogger('telegram.vendor.ptb_urllib3.urllib3.connectionpool').setLevel(logging.CRITICAL)

        # Set up logger
        self.logger: logging.Logger = logging.getLogger(__name__)

        # Load configs
        self.load_config()

        # Load predefined MIME types
        mimetypes.init(files=["mimetypes"])

        # Initialize managers
        self.flag: ExperimentalFlagsManager = ExperimentalFlagsManager(self)
        self.db: DatabaseManager = DatabaseManager(self)
        self.bot_manager: TelegramBotManager = TelegramBotManager(self)
        # self.voice_recognition: VoiceRecognitionManager = VoiceRecognitionManager(self)
        self.chat_binding: ChatBindingManager = ChatBindingManager(self)
        self.commands: CommandsManager = CommandsManager(self)
        self.master_messages: MasterMessageProcessor = MasterMessageProcessor(self)
        self.slave_messages: SlaveMessageProcessor = SlaveMessageProcessor(self)

        if not self.flag('auto_locale'):
            self.translator = translation("efb_telegram_master",
                                          resource_filename('efb_telegram_master', 'locale'),
                                          fallback=True)

        # Basic message handlers
        self.bot_manager.dispatcher.add_handler(
            GlobalCommandHandler("start", self.start, pass_args=True))
        self.bot_manager.dispatcher.add_handler(
            telegram.ext.CommandHandler("help", self.help))
        self.bot_manager.dispatcher.add_handler(
            GlobalCommandHandler("info", self.info))
        self.bot_manager.dispatcher.add_handler(
            telegram.ext.CallbackQueryHandler(self.bot_manager.session_expired))

        self.bot_manager.dispatcher.add_error_handler(self.error)

        self.rpc_utilities = RPCUtilities(self)

    @property
    def _(self):
        return self.translator.gettext

    @property
    def ngettext(self):
        return self.translator.ngettext

    def load_config(self):
        """
        Load configuration from path specified by the framework.
        
        Configuration file is in YAML format.
        """
        config_path = efb_utils.get_config_path(self.channel_id)
        if not config_path.exists():
            raise FileNotFoundError(self._("Config File does not exist. ({path})").format(path=config_path))
        with open(config_path) as f:
            data = yaml.load(f)

            # Verify configuration
            if not isinstance(data.get('token', None), str):
                raise ValueError(self._('Telegram bot token must be a string'))
            if isinstance(data.get('admins', None), int):
                data['admins'] = [data['admins']]
            if isinstance(data.get('admins', None), str) and data['admins'].isdigit():
                data['admins'] = [int(data['admins'])]
            if not isinstance(data.get('admins', None), list) or len(data['admins']) < 1:
                raise ValueError(self._("Admins' user IDs must be a list of one number or more."))
            for i in range(len(data['admins'])):
                if isinstance(data['admins'][i], str) and data['admins'][i].isdigit():
                    data['admins'][i] = int(data['admins'][i])
                if not isinstance(data['admins'][i], int):
                    raise ValueError(self._('Admin ID is expected to be an int, but {data} is found.')
                                     .format(data=data['admins'][i]))

            self.config = data.copy()

    def info(self, bot, update):
        """
        Show info of the current telegram conversation.
        Triggered by `/info`.

        Args:
            bot: Telegram Bot instance
            update: Message update
        """
        if update.message.chat.type != telegram.Chat.PRIVATE:  # Group message
            links = self.db.get_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, update.message.chat_id))
            if links:  # Linked chat
                msg = self._("The group {group_name} ({group_id}) is linked to:").format(
                    group_name=update.message.chat.title,
                    group_id=update.message.chat_id)
                for i in links:
                    channel_id, chat_id = etm_utils.chat_id_str_to_id(i)
                    d = self.chat_binding.get_chat_from_db(channel_id, chat_id)
                    if d:
                        msg += "\n- %s (%s:%s)" % (ETMChat(chat=d, db=self.db).full_name,
                                                   d.module_id, d.chat_uid)
                    else:
                        if channel_id not in coordinator.slaves:
                            msg += self._("\n- Unknown channel {channel_id}: {chat_id}").format(
                                channel_id=channel_id,
                                chat_id=chat_id
                            )
                        else:
                            msg += self._("\n- {channel_emoji} {channel_name}: Unknown chat ({chat_id})").format(
                                channel_emoji=coordinator.slaves[channel_id].channel_emoji,
                                channel_name=coordinator.slaves[channel_id].channel_name,
                                chat_id=chat_id
                            )
            else:
                msg = self._("The group {group_name} ({group_id}) is not linked to any remote chat. "
                             "To link one, use /link.").format(group_name=update.message.chat.title,
                                                               group_id=update.message.chat_id)
        elif update.effective_message.forward_from_chat and \
                update.effective_message.forward_from_chat.type == 'channel':  # Forwarded channel command.
            chat = update.effective_message.forward_from_chat
            links = self.db.get_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, chat.id))
            if links:  # Linked chat
                msg = self._("The channel {group_name} ({group_id}) is linked to:") \
                    .format(group_name=chat.title,
                            group_id=chat.id)
                for i in links:
                    channel_id, chat_id = etm_utils.chat_id_str_to_id(i)
                    d = self.chat_binding.get_chat_from_db(channel_id, chat_id)
                    if d:
                        msg += "\n- %s" % ETMChat(chat=d, db=self.db).full_name
                    else:
                        msg += self._("\n- {channel_emoji} {channel_name}: Unknown chat ({chat_id})").format(
                            channel_emoji=coordinator.slaves[channel_id].channel_emoji,
                            channel_name=coordinator.slaves[channel_id].channel_name,
                            chat_id=chat_id
                        )
            else:
                msg = self._("The channel {group_name} ({group_id}) is "
                             "not linked to any remote chat. ").format(group_name=chat.title,
                                                                       group_id=chat.id)
        else:  # Talking to the bot.
            msg = self.ngettext("This is EFB Telegram Master Channel {version}.\n"
                                "{count} slave channel activated:",
                                "This is EFB Telegram Master Channel {version}.\n"
                                "{count} slave channels activated:",
                                len(coordinator.slaves)).format(
                version=self.__version__, count=len(coordinator.slaves))
            for i in coordinator.slaves:
                msg += "\n- %s %s (%s, %s)" % (coordinator.slaves[i].channel_emoji,
                                               coordinator.slaves[i].channel_name,
                                               i, coordinator.slaves[i].__version__)
            if coordinator.middlewares:
                msg += self.ngettext("\n\n{count} middleware activated:", "\n\n{count} middlewares activated:",
                                     len(coordinator.middlewares)).format(count=len(coordinator.middlewares))
                for i in coordinator.middlewares:
                    msg += "\n- %s (%s, %s)" % (i.middleware_name, i.middleware_id, i.__version__)

        update.message.reply_text(msg)

    def start(self, bot, update, args=None):
        """
        Process bot command `/start`.

        Args:
            bot: Telegram Bot instance
            update (telegram.Update): Message update
            args: Arguments from message
        """
        if args:  # Group binding command
            if update.effective_message.chat.type != telegram.Chat.PRIVATE or \
                    (update.effective_message.forward_from_chat and
                     update.effective_message.forward_from_chat.type == telegram.Chat.CHANNEL):
                self.chat_binding.link_chat(update, args)
            else:
                self.bot_manager.send_message(update.effective_chat.id,
                                              self._('You cannot link remote chats to here. Please try again.'))
        else:
            txt = self._("This is EFB Telegram Master Channel.\n\n"
                         "To learn more, please visit https://github.com/blueset/efb-telegram-master .")
            self.bot_manager.send_message(update.effective_chat.id, txt)

    def help(self, bot, update):
        txt = self._("EFB Telegram Master Channel\n"
                     "/link\n"
                     "    Link a remote chat to an empty Telegram group.\n"
                     "    Followed by a regular expression to filter results.\n"
                     "/chat\n"
                     "    Generate a chat head to start a conversation.\n"
                     "    Followed by a regular expression to filter results.\n"
                     "/extra\n"
                     "    List all additional features from slave channels.\n"
                     "/unlink_all\n"
                     "    Unlink all remote chats in this chat.\n"
                     "/info\n"
                     "    Show information of the current Telegram chat.\n"
                     "/update_info\n"
                     "    Update name and profile picture a linked Telegram group.\n"
                     "    Only works in singly linked group where the bot is an admin.\n"
                     "/help\n"
                     "    Print this command list.")
        bot.send_message(update.message.from_user.id, txt)

    def poll(self):
        """
        Message polling process.
        """

        self.bot_manager.polling()

    def error(self, bot, update, error):
        """
        Print error to console, and send error message to first admin.
        Triggered by python-telegram-bot error callback.
        """
        if "(409)" in str(error):
            msg = self._('Conflicted polling detected. If this error persists, '
                         'please ensure you are running only one instance of this Telegram bot.')
            self.logger.critical(msg)
            self.bot_manager.send_message(self.config['admins'][0], msg)
            return
        if "Invalid server response" in str(error) and not update:
            self.logger.error("Boom! Telegram API is no good. (Invalid server response.)")
            return
        try:
            raise error
        except telegram.error.Unauthorized:
            self.logger.error("The bot is not authorised to send update:\n%s\n%s", str(update), str(error))
        except telegram.error.BadRequest as e:
            if e.message == "Message is not modified" and update.callback_query:
                self.logger.error("Chill bro, don't click that fast.")
            else:
                self.logger.error("Message request is invalid.\n%s\n%s", str(update), str(error))
                self.bot_manager.send_message(self.config['admins'][0],
                                              self._("Message request is invalid.\n{error}\n"
                                                     "<code>{update}</code>").format(
                                                  error=html.escape(str(error)), update=html.escape(str(update))),
                                              parse_mode="HTML")
        except (telegram.error.TimedOut, telegram.error.NetworkError):
            self.timeout_count += 1
            self.logger.error("Poor internet connection detected.\n"
                              "Number of network error occurred since last startup: %s\n\%s\nUpdate: %s",
                              self.timeout_count, str(error), str(update))
            if update is not None and isinstance(getattr(update, "message", None), telegram.Message):
                update.message.reply_text(self._("This message is not processed due to poor internet environment "
                                                 "of the server.\n"
                                                 "<code>{code}</code>").format(code=html.escape(str(error))),
                                          quote=True,
                                          parse_mode="HTML")

            timeout_interval = self.flag('network_error_prompt_interval')
            if timeout_interval > 0 and self.timeout_count % timeout_interval == 0:
                self.bot_manager.send_message(self.config['admins'][0],
                                              self.ngettext("<b>EFB Telegram Master channel</b>\n"
                                                            "You may have a poor internet connection on your server. "
                                                            "Currently {count} network error is detected.\n"
                                                            "For more details, please refer to the log.",
                                                            "<b>EFB Telegram Master channel</b>\n"
                                                            "You may have a poor internet connection on your server. "
                                                            "Currently {count} network errors are detected.\n"
                                                            "For more details, please refer to the log.",
                                                            self.timeout_count).format(
                                                  count=self.timeout_count),
                                              parse_mode="HTML")
        except telegram.error.ChatMigrated as e:
            new_id = e.new_chat_id
            old_id = update.message.chat_id
            count = 0
            for i in self.db.get_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, old_id)):
                self.logger.debug('Migrating slave chat %s from Telegram chat %s to %s.', i, old_id, new_id)
                self.db.remove_chat_assoc(slave_uid=i)
                self.db.add_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, new_id), slave_uid=i)
                count += 1
            bot.send_message(new_id, self.ngettext("Chat migration detected.\n"
                                                   "All {count} remote chat are now linked to this new group.",
                                                   "Chat migration detected.\n"
                                                   "All {count} remote chats are now linked to this new group.",
                                                   count).format(count=count))
        except Exception as e:
            try:
                bot.send_message(self.config['admins'][0],
                                 self._("EFB Telegram Master channel encountered error <code>{error}</code> "
                                        "caused by update <code>{update}</code>.").format(error=html.escape(str(error)),
                                                                                          update=html.escape(
                                                                                              str(update))),
                                 parse_mode="HTML")
            except Exception as ex:
                self.logger.exception("Failed to send error message through Telegram: %s", ex)

            finally:
                self.logger.exception('Unhandled telegram bot error!\n'
                                      'Update %s caused error %s. Exception', update, error, e)

    def send_message(self, msg: EFBMsg) -> EFBMsg:
        return self.slave_messages.send_message(msg)

    def send_status(self, status: EFBStatus):
        return self.slave_messages.send_status(status)

    def get_message_by_id(self, msg_id: str) -> Optional['EFBMsg']:
        # TODO: implement this method
        pass

    def stop_polling(self):
        self.logger.debug("Gracefully stopping %s (%s).", self.channel_name, self.channel_id)
        self.rpc_utilities.shutdown()
        self.bot_manager.graceful_stop()
        self.logger.debug("%s (%s) gracefully stopped.", self.channel_name, self.channel_id)
