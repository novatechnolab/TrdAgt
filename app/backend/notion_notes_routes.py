import os
import json
import sqlite3
import datetime
import requests
from flask import Blueprint, request, jsonify, g

notion_notes_bp = Blueprint('notion_notes_bp', __name__)

# DB path helper
_backend_dir = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_backend_dir, 'tradesignal_cache.db')

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# Helper to split note body into Notion paragraph blocks
def convert_to_notion_blocks(markdown_text):
    blocks = []
    lines = markdown_text.split('\n')
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        
        # Simple heading detection
        if stripped.startswith('### '):
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {
                    "rich_text": [{"type": "text", "text": {"content": stripped[4:]}}]
                }
            })
        elif stripped.startswith('## '):
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": stripped[3:]}}]
                }
            })
        elif stripped.startswith('# '):
            blocks.append({
                "object": "block",
                "type": "heading_1",
                "heading_1": {
                    "rich_text": [{"type": "text", "text": {"content": stripped[2:]}}]
                }
            })
        elif stripped.startswith('- ') or stripped.startswith('* '):
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [{"type": "text", "text": {"content": stripped[2:]}}]
                }
            })
        else:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": stripped}}]
                }
            })
    
    # Cap blocks to Notion's max limit of 100 blocks per request
    return blocks[:99]


# ── NOTE ENDPOINTS ──

@notion_notes_bp.route('/api/notes', methods=['GET'])
def get_notes():
    """Fetch all local notes, sorted by updated_at descending."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, title, content, symbol, sentiment, created_at, updated_at, notion_page_id, sync_status 
            FROM notes 
            ORDER BY datetime(updated_at) DESC
        ''')
        rows = cursor.fetchall()
        notes_list = [dict(row) for row in rows]
        conn.close()
        return jsonify(notes_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@notion_notes_bp.route('/api/notes', methods=['POST'])
def save_note():
    """Create or update a local note."""
    try:
        data = request.json or {}
        note_id = data.get('id')
        title = data.get('title', '').strip() or 'Untitled Note'
        content = data.get('content', '')
        symbol = data.get('symbol', '').strip().upper() or None
        sentiment = data.get('sentiment', 'NEUTRAL').strip().upper()
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        conn = get_db_connection()
        cursor = conn.cursor()

        if note_id:
            # Update existing
            cursor.execute('''
                UPDATE notes 
                SET title = ?, content = ?, symbol = ?, sentiment = ?, updated_at = ?, sync_status = 'PENDING'
                WHERE id = ?
            ''', (title, content, symbol, sentiment, now_str, note_id))
            conn.commit()
            
            # Fetch updated note
            cursor.execute('SELECT * FROM notes WHERE id = ?', (note_id,))
            updated_note = dict(cursor.fetchone())
            conn.close()
            return jsonify({"success": True, "note": updated_note})
        else:
            # Create new
            cursor.execute('''
                INSERT INTO notes (title, content, symbol, sentiment, created_at, updated_at, sync_status)
                VALUES (?, ?, ?, ?, ?, ?, 'PENDING')
            ''', (title, content, symbol, sentiment, now_str, now_str))
            new_id = cursor.lastrowid
            conn.commit()
            
            # Fetch created note
            cursor.execute('SELECT * FROM notes WHERE id = ?', (new_id,))
            new_note = dict(cursor.fetchone())
            conn.close()
            return jsonify({"success": True, "note": new_note})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@notion_notes_bp.route('/api/notes/<int:note_id>', methods=['DELETE'])
def delete_note(note_id):
    """Delete a note locally."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM notes WHERE id = ?', (note_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── NOTION INTEGRATION ENDPOINTS ──

@notion_notes_bp.route('/api/notion/config', methods=['GET'])
def get_notion_config():
    """Retrieve Notion API credentials securely."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT key, value FROM notion_config')
        rows = cursor.fetchall()
        conn.close()

        config = {row['key']: row['value'] for row in rows}
        # Mask API Key for security if returning to client
        masked_config = {
            "api_key": "***" + config.get("api_key", "")[-4:] if config.get("api_key") else "",
            "parent_id": config.get("parent_id", ""),
            "parent_type": config.get("parent_type", "database"), # database or page
            "has_key": bool(config.get("api_key"))
        }
        return jsonify(masked_config)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@notion_notes_bp.route('/api/notion/config', methods=['POST'])
def save_notion_config():
    """Save Notion API credentials locally."""
    try:
        data = request.json or {}
        api_key = data.get('api_key', '').strip()
        parent_id = data.get('parent_id', '').strip()
        parent_type = data.get('parent_type', 'database').strip().lower()

        conn = get_db_connection()
        cursor = conn.cursor()

        # Update or insert
        if api_key:
            # If the user didn't overwrite the masked key
            if not api_key.startswith('***'):
                cursor.execute('INSERT OR REPLACE INTO notion_config (key, value) VALUES ("api_key", ?)', (api_key,))
        
        cursor.execute('INSERT OR REPLACE INTO notion_config (key, value) VALUES ("parent_id", ?)', (parent_id,))
        cursor.execute('INSERT OR REPLACE INTO notion_config (key, value) VALUES ("parent_type", ?)', (parent_type,))
        
        conn.commit()
        conn.close()
        return jsonify({"success": True, "message": "Notion settings saved successfully!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@notion_notes_bp.route('/api/notion/test-connection', methods=['POST'])
def test_connection():
    """Test connection with the configured Notion parent (Database or Page)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT key, value FROM notion_config')
        rows = cursor.fetchall()
        conn.close()

        config = {row['key']: row['value'] for row in rows}
        api_key = config.get('api_key')
        parent_id = config.get('parent_id')
        parent_type = config.get('parent_type', 'database')

        # Allow user to pass credentials temporarily in test request
        req_data = request.json or {}
        test_key = req_data.get('api_key', '').strip()
        test_parent = req_data.get('parent_id', '').strip()
        test_type = req_data.get('parent_type', 'database').strip().lower()

        if test_key and not test_key.startswith('***'):
            api_key = test_key
        if test_parent:
            parent_id = test_parent
            parent_type = test_type

        if not api_key or not parent_id:
            return jsonify({"success": False, "error": "Notion Token or Parent ID is missing."}), 400

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        }

        # Check connectivity depending on parent type
        if parent_type == 'database':
            url = f"https://api.notion.com/v1/databases/{parent_id}"
        else:
            url = f"https://api.notion.com/v1/pages/{parent_id}"

        res = requests.get(url, headers=headers, timeout=10)
        
        if res.status_code == 200:
            res_data = res.json()
            title_prop = res_data.get('title')
            # Extract database title or page title
            title_text = "Connected"
            if title_prop and isinstance(title_prop, list) and len(title_prop) > 0:
                title_text = title_prop[0].get('plain_text', 'Notion Item')
            elif isinstance(title_prop, dict) and 'title' in title_prop:
                # Page properties
                pass
            
            return jsonify({
                "success": True, 
                "message": f"Successfully connected to Notion {parent_type.capitalize()}!",
                "title": title_text
            })
        else:
            err_msg = res.json().get('message', f"HTTP {res.status_code}")
            return jsonify({"success": False, "error": f"Notion Error: {err_msg}"}), 400

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@notion_notes_bp.route('/api/notes/<int:note_id>/sync', methods=['POST'])
def sync_note_to_notion(note_id):
    """Publish/Sync a note directly to Notion as a database item or sub-page."""
    try:
        # Load local note
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM notes WHERE id = ?', (note_id,))
        note_row = cursor.fetchone()
        
        if not note_row:
            conn.close()
            return jsonify({"error": "Note not found."}), 404
        
        note = dict(note_row)

        # Load Notion settings
        cursor.execute('SELECT key, value FROM notion_config')
        rows = cursor.fetchall()
        conn.close()

        config = {row['key']: row['value'] for row in rows}
        api_key = config.get('api_key')
        parent_id = config.get('parent_id')
        parent_type = config.get('parent_type', 'database')

        if not api_key or not parent_id:
            return jsonify({"error": "Notion is not configured. Please complete settings first."}), 400

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        }

        # Convert note body into Notion block children
        body_blocks = convert_to_notion_blocks(note['content'])

        # Payload construction
        payload = {}
        
        if parent_type == 'database':
            # Create Database Page
            payload = {
                "parent": {"database_id": parent_id},
                "properties": {
                    "Name": {
                        "title": [{"text": {"content": note['title']}}]
                    }
                },
                "children": body_blocks
            }
            
            # Optional structured columns if database has them:
            # We wrap in try-blocks or keep standard since basic titles are always supported.
            # To be highly robust and avoid database schema errors, we only add these if they match standard schemas.
            # Let's add them as tags / custom text.
            if note['symbol']:
                payload["properties"]["Symbol"] = {
                    "rich_text": [{"text": {"content": note['symbol']}}]
                }
            if note['sentiment']:
                payload["properties"]["Sentiment"] = {
                    "select": {"name": note['sentiment']}
                }
            
            # We can format date using ISO
            now_iso = datetime.datetime.now().strftime('%Y-%m-%d')
            payload["properties"]["Date"] = {
                "date": {"start": now_iso}
            }

        else:
            # Create Sub-Page under Parent Page
            payload = {
                "parent": {"page_id": parent_id},
                "properties": {
                    "title": [{"text": {"content": note['title']}}]
                },
                "children": body_blocks
            }

        # Issue Notion request
        res = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload, timeout=15)
        
        if res.status_code == 200:
            res_data = res.json()
            notion_page_id = res_data.get('id')
            
            # Update local note state
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE notes 
                SET notion_page_id = ?, sync_status = 'SYNCED', updated_at = ?
                WHERE id = ?
            ''', (notion_page_id, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), note_id))
            conn.commit()
            conn.close()

            return jsonify({
                "success": True, 
                "notion_page_id": notion_page_id,
                "message": "Note successfully synced to Notion!"
            })
        else:
            # Notion rejected structural payload, let's retry with simplified fallback properties!
            # If the database has custom columns or missing columns, a full property sync might fail.
            # Fall back to only "Name" title property to ensure sync success!
            if parent_type == 'database':
                fallback_payload = {
                    "parent": {"database_id": parent_id},
                    "properties": {
                        "Name": {
                            "title": [{"text": {"content": note['title']}}]
                        }
                    },
                    "children": body_blocks
                }
                
                res2 = requests.post("https://api.notion.com/v1/pages", headers=headers, json=fallback_payload, timeout=15)
                if res2.status_code == 200:
                    res_data = res2.json()
                    notion_page_id = res_data.get('id')
                    
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE notes 
                        SET notion_page_id = ?, sync_status = 'SYNCED', updated_at = ?
                        WHERE id = ?
                    ''', (notion_page_id, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), note_id))
                    conn.commit()
                    conn.close()
                    
                    return jsonify({
                        "success": True, 
                        "notion_page_id": notion_page_id,
                        "message": "Synced successfully to Notion Database (Simple schema fallback applied)."
                    })

            err_data = res.json()
            err_msg = err_data.get('message', f"HTTP {res.status_code}")
            return jsonify({"error": f"Notion API error: {err_msg}"}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@notion_notes_bp.route('/api/notes/upload-image', methods=['POST'])
def upload_image():
    """Upload a pasted image and save it locally in the app static directory."""
    import uuid
    try:
        if 'image' not in request.files:
            return jsonify({"error": "No image file provided in the request."}), 400
        
        file = request.files['image']
        if file.filename == '':
            return jsonify({"error": "Selected file has no filename."}), 400
        
        # Resolve dynamic static_folder from the current Flask app instance
        from flask import current_app
        static_dir = current_app.static_folder
        if not static_dir:
            # Fallback
            static_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
            
        uploads_dir = os.path.join(static_dir, 'uploads')
        os.makedirs(uploads_dir, exist_ok=True)
        
        # Save file with safe UUID filename to prevent collisions
        ext = os.path.splitext(file.filename)[1] or '.png'
        filename = f"pasted_{uuid.uuid4().hex}{ext}"
        filepath = os.path.join(uploads_dir, filename)
        
        file.save(filepath)
        
        # Return URL relative to server root
        url = f"/uploads/{filename}"
        return jsonify({"success": True, "url": url})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500
