"""Global bot-owner preferences; independent of user records and chat history."""
from storage import read_json, write_json
import config

DEFAULTS = {'mode': 'inline', 'streaming': True, 'style': 'balanced',
            'reasoning': 'medium', 'math': 'rich'}

def get_settings(context):
    return dict(DEFAULTS, **context.application.bot_data.get('global_settings', {}))

def initialize(application):
    saved = read_json(config.DATA_DIR / 'global_settings.json', dict)
    settings = dict(DEFAULTS)
    for key, values in {'mode': ('inline','guest','off'), 'style': ('balanced','concise','detailed'),
                        'reasoning': ('low','medium','high'), 'math': ('rich','unicode')}.items():
        if saved.get(key) in values: settings[key] = saved[key]
    if isinstance(saved.get('streaming'), bool): settings['streaming'] = saved['streaming']
    application.bot_data['global_settings'] = settings

def save_settings(context, settings):
    write_json(config.DATA_DIR / 'global_settings.json', settings)
    context.application.bot_data['global_settings'] = dict(settings)

def enabled(context, mode):
    return get_settings(context)['mode'] == mode
