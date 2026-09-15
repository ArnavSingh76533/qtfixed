"""Restart-safe conversation history and settings, isolated by chat/topic/user."""
import json
import sqlite3

DEFAULTS = {'streaming': True, 'style': 'balanced', 'reasoning': 'medium', 'math': 'image'}


class ChatStore:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS chats (key TEXT PRIMARY KEY, history TEXT NOT NULL, settings TEXT NOT NULL)')
        self.db.commit()

    def get(self, key):
        row = self.db.execute('SELECT history, settings FROM chats WHERE key=?', (key,)).fetchone()
        if not row:
            return [], DEFAULTS.copy()
        return json.loads(row[0]), {**DEFAULTS, **json.loads(row[1])}

    def save(self, key, history, settings):
        with self.db:
            self.db.execute('INSERT INTO chats VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET history=excluded.history, settings=excluded.settings',
                            (key, json.dumps(history, ensure_ascii=False), json.dumps(settings)))

    def clear(self, key, forget=False):
        if forget:
            with self.db:
                self.db.execute('DELETE FROM chats WHERE key=?', (key,))
        else:
            self.save(key, [], self.get(key)[1])

    def close(self):
        self.db.close()
