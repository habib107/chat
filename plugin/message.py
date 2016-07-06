from psycopg2.extensions import AsIs

import skygear
from skygear.utils import db
from skygear.utils.context import current_user_id

from .asset import sign_asset_url
from .exc import SkygearChatException
from .pubsub import _publish_event
from .utils import _get_conversation, schema_name


@skygear.before_save("message", async=False)
def handle_message_before_save(record, original_record, conn):
    conversation = _get_conversation(record['conversation_id'])

    if current_user_id() not in conversation['participant_ids']:
        raise SkygearChatException("user not in conversation")

    if original_record is not None:
        raise SkygearChatException("message is not editable")


@skygear.after_save("message")
def handle_message_after_save(record, original_record, conn):
    conversation = _get_conversation(record['conversation_id'])
    for p_id in conversation['participant_ids']:
        _publish_event(
            p_id, "message", "create", record)


@skygear.before_save("last_message_read", async=False)
def handle_last_message_read_before_save(record, original_record, conn):
    new_id = record.get('message_id')
    if new_id is None:
        return

    old_id = original_record and original_record.get('message_id')
    conversation_id = record['conversation_id']

    cur = conn.execute('''
        SELECT _id, _created_at
        FROM %(schema_name)s.message
        WHERE (_id = %(new_id)s OR _id = %(old_id)s)
        AND conversation_id = %(conversation_id)s
        LIMIT 2;
        ''', {
        'schema_name': AsIs(schema_name),
        'new_id': new_id,
        'old_id': old_id,
        'conversation_id': conversation_id
    }
    )

    results = {}
    for row in cur:
        results[row[0]] = row[1]

    if new_id not in results:
        raise SkygearChatException("no message found")

    if old_id and results[new_id] < results[old_id]:
        raise SkygearChatException("the updated message is older")


@skygear.op("chat:get_messages", auth_required=True, user_required=True)
def get_messages(conversation_id, limit, before_time=None):
    conversation = _get_conversation(conversation_id)

    if current_user_id() not in conversation['participant_ids']:
        raise SkygearChatException("user not in conversation")

    # FIXME: After the ACL can be by-pass the ACL, we should query the with
    # master key
    # https://github.com/SkygearIO/skygear-server/issues/51
    with db.conn() as conn:
        cur = conn.execute('''
            SELECT
                _id, _created_at, _created_by,
                body, conversation_id, metadata, attachment
            FROM %(schema_name)s.message
            WHERE conversation_id = %(conversation_id)s
            AND (_created_at < %(before_time)s OR %(before_time)s IS NULL)
            ORDER BY _created_at DESC
            LIMIT %(limit)s;
            ''', {
            'schema_name': AsIs(schema_name),
            'conversation_id': conversation_id,
            'before_time': before_time,
            'limit': limit
        }
        )

        results = []
        for row in cur:
            r = {
                '_id': row[0],
                '_created_at': row[1].isoformat(),
                '_created_by': row[2],
                'body': row[3],
                'conversation_id': row[4],
                'metadata': row[5],
            }
            if row[6]:
                r['attachment'] = {
                    '$type': 'asset',
                    '$name': row[6],
                    '$url': sign_asset_url(row[6])
                }
            results.append(r)

        return {'results': results}


@skygear.op("chat:get_unread_message_count",
            auth_required=True, user_required=True)
def get_unread_message_count(conversation_id):
    with db.conn() as conn:
        cur = conn.execute('''
            SELECT message_id
            FROM %(schema_name)s.last_message_read
            WHERE conversation_id = %(conversation_id)s
            AND _database_id = %(user_id)s
            LIMIT 1;
            ''', {
            'schema_name': AsIs(schema_name),
            'conversation_id': conversation_id,
            'user_id': current_user_id()
        }
        )

        results = [row[0] for row in cur]

        if results:
            message_id = results[0]
        else:
            message_id = None

        if message_id:
            cur = conn.execute('''
                SELECT COUNT(*)
                FROM %(schema_name)s.message
                WHERE _created_at > (
                    SELECT _created_at
                    FROM %(schema_name)s.message
                    WHERE _id = %(message_id)s
                )
                AND conversation_id = %(conversation_id)s
                ''', {
                'schema_name': AsIs(schema_name),
                'message_id': message_id,
                'conversation_id': conversation_id
            }
            )
        else:
            cur = conn.execute('''
                SELECT COUNT(*)
                FROM %(schema_name)s.message
                WHERE conversation_id = %(conversation_id)s
                ''', {
                'schema_name': AsIs(schema_name),
                'conversation_id': conversation_id
            }
            )

    return {'count': [row[0] for row in cur][0]}