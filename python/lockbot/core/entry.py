r"""Legacy Flask entry point for standalone bot deployment."""

from flask import Flask, request

from lockbot.core.bot_instance import BotInstance
from lockbot.core.config import Config
from lockbot.core.handler import handle_request, page_not_found


def create_app(bot, bot_name, port=8090):
    """Create a Flask app that handles bot webhook requests.

    Args:
        bot: Bot instance (NodeBot / DeviceBot / QueueBot).
        bot_name (str): Bot name.
        port (int, optional): Server port. Defaults to 8090.

    Returns:
        flask.Flask: The configured Flask application.
    """
    app = Flask(bot_name)

    @app.route("/", methods=["POST"])
    def serve():
        echostr = request.form.get("echostr", None)
        if echostr:
            signature = request.form.get("signature", None)
            rn = request.form.get("rn", None)
            timestamp = request.form.get("timestamp", None)
            msg_base64 = None
        else:
            signature = request.args.get("signature", None)
            rn = request.args.get("rn", None)
            timestamp = request.args.get("timestamp", None)
            msg_base64 = request.get_data()
        return handle_request(echostr, signature, rn, timestamp, msg_base64, bot)

    @app.errorhandler(404)
    def page404(_):
        return page_not_found(_)

    return app


def run_bot():
    """Load config, create a BotInstance from global Config, and start the server.

    Legacy entry point: compatible with servers/*.py that call Config.set() first.
    """
    Config.load_from_file()
    Config.load_from_env()
    Config.show_all()

    bot_type = Config.get("BOT_TYPE")
    config_dict = Config.get_all()
    instance = BotInstance(bot_type, config_dict)
    bot = instance.bot

    port = Config.get("PORT")

    app = create_app(
        bot=bot,
        bot_name=Config.get("BOT_NAME"),
        port=port,
    )
    app.run(host="0.0.0.0", port=port)
