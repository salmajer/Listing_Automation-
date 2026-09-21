"""
This service is a Flask API for Tinza, a multi-vendor marketplace
automation platform built on WooCommerce/Dokan. It sits between an n8n
automation pipeline (which handles LLM extraction, scheduling, etc.) and:

  - A Postgres database (store credentials / webhook channel bookkeeping)
  - Google Drive (reading uploaded product files: Google Docs / .docx)
  - Supabase Storage (hosting compressed product images)
  - A WooCommerce REST API (creating products + variations on a store)

It exists mainly because sensitive operations (decrypting store credentials,
talking to Drive with a service account, etc.) need to happen outside of the
n8n workflow itself, which cannot safely access secrets/env vars in some
node types.

Endpoints:
  POST /onboard        - Register a new vendor/store + its Drive webhook channel
  POST /upload-media    - Compress and upload a product image to Supabase Storage
  POST /create-product   - Create a WooCommerce product (simple or variable) from
                            structured data extracted upstream by the LLM pipeline
  POST /extract-text     - Pull raw text out of a Google Doc or .docx file on Drive
"""

import io
import os
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
import psycopg2
import requests
from docx import Document
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import itertools
from PIL import Image
from supabase import create_client, Client
import base64
import traceback
from functools import lru_cache
import concurrent.futures
from pillow_heif import register_heif_opener
import pillow_avif 


register_heif_opener()
load_dotenv()  # Load environment variables from a local .env file, if present

app = Flask(__name__)

# --- CONFIGURATION ---

# Symmetric key used to encrypt/decrypt WooCommerce credentials at rest in
# the database. Stored as a hex string in the environment and converted to
# raw bytes here.
AES_KEY = bytes.fromhex(os.environ.get('AES_KEY'))

# Postgres connection details for the "stores"/"webhook_channels" tables
# (vendor onboarding + credential storage, not the n8n internal DB).
DB_HOST = os.environ.get('DB_HOST')
DB_PORT = int(os.environ.get('DB_PORT', 5432))
DB_NAME = os.environ.get('DB_NAME')
DB_USER = os.environ.get('DB_USER')
DB_PASS = os.environ.get('DB_PASS')

# Supabase Storage bucket where compressed product images are uploaded.
SUPABASE_BUCKET_NAME = os.environ.get('SUPABASE_BUCKET_NAME', 'media')

# Base URL of the WooCommerce store products are created on.
WC_BASE_URL = os.environ.get('WC_BASE_URL')

# Google service-account credentials used to read files from Drive
# (read-only access is enough since this API never writes back to Drive).
SERVICE_ACCOUNT_FILE = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', '/secrets/sa-key.json')
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

# Supabase client used for both Storage (images) and any table access.
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# --- UTILS ---

def encrypt(text):
    """
    Encrypt a plaintext string (e.g. a WooCommerce username/password) using
    AES-256 in CBC mode with a random IV and manual PKCS#7-style padding.

    Returns a string of the form "<iv_hex>:<ciphertext_hex>" so the IV can
    be recovered later for decryption.
    """
    iv = os.urandom(16)  # Random 16-byte initialization vector per encryption
    cipher = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()

    text_bytes = text.encode('utf-8')
    # Pad the plaintext to a multiple of the AES block size (16 bytes)
    pad_len = 16 - len(text_bytes) % 16
    text_bytes += bytes([pad_len] * pad_len)

    encrypted = encryptor.update(text_bytes) + encryptor.finalize()
    return iv.hex() + ':' + encrypted.hex()


def decrypt_value(encrypted_text):
    """
    Reverse of encrypt(): takes an "<iv_hex>:<ciphertext_hex>" string,
    decrypts it with AES-256-CBC, strips the padding, and returns the
    original plaintext string.
    """
    iv_hex, enc_hex = encrypted_text.split(':')
    iv = bytes.fromhex(iv_hex)
    encrypted = bytes.fromhex(enc_hex)

    cipher = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(encrypted) + decryptor.finalize()

    pad_len = decrypted[-1]  # Last byte tells us how many padding bytes to strip
    return decrypted[:-pad_len].decode('utf-8')


def get_db_connection():
    """Open a new connection to the Postgres database."""
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS, sslmode='require'
    )


def get_drive_service():
    """Build an authenticated Google Drive API client using the service account."""
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return build('drive', 'v3', credentials=creds)


@lru_cache(maxsize=100)
def get_store_credentials(store_id):
    """
    Look up and decrypt the WooCommerce username/password for a given
    store_id. Results are cached in-process (up to 100 stores) since
    credentials rarely change and this avoids a DB round trip on every
    product creation request.

    Returns (wc_user, wc_pass), or (None, None) if the store doesn't exist.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT wc_user, wc_pass FROM stores WHERE id = %s', (store_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return decrypt_value(row[0]), decrypt_value(row[1])
    return None, None


@lru_cache(maxsize=500)
def get_wc_category(store_url, wc_user, wc_pass, category_name):
    """
    Resolve a category name (as extracted by the LLM) to a WooCommerce
    category ID by searching the store's existing categories.

    Prefers an exact (case-insensitive) name match; otherwise falls back to
    the first search result. Cached since the same category names get
    looked up repeatedly across many products.

    Returns a list like [{'id': <category_id>}], or [] if nothing matched.
    """
    res = requests.get(
        f'{store_url}/wp-json/wc/v3/products/categories',
        auth=(wc_user, wc_pass),
        params={'search': category_name, 'per_page': 100}
    )
    if res.ok and res.json():
        categories = res.json()
        exact_cat = next((c for c in categories if c['name'].lower() == category_name.lower()), categories[0])
        return [{'id': exact_cat['id']}]
    return []


# --- ROUTES ---

@app.route('/onboard', methods=['POST'])
def onboard():
    """
    Register a new vendor/store.

    Expects JSON with: wc_user, wc_pass, store_name, default_status,
    drive_folder_id, channel_id, resource_id, expires_at, webhook_url.

    Steps:
      1. Encrypt the WooCommerce credentials before storing them.
      2. Insert a new row into `stores`.
      3. Insert a matching row into `webhook_channels` recording the Google
         Drive push-notification channel used to watch that store's folder
         (expires_at is passed in as epoch milliseconds and converted to a
         timestamp).

    Both inserts happen on the same connection/transaction and are
    committed together.
    """
    data = request.get_json(silent=True) or {}
    try:
        wc_user_encrypted = encrypt(data['wc_user'])
        wc_pass_encrypted = encrypt(data['wc_pass'])
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO stores (store_name, wc_user, wc_pass, default_status, drive_folder_id)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (
            data['store_name'],
            wc_user_encrypted,
            wc_pass_encrypted,
            data['default_status'],
            data['drive_folder_id']
        ))
        row = cur.fetchone()
        store_id = str(row[0]) if row else 'unknown'
        cur.execute("""
            INSERT INTO webhook_channels (store_id, channel_id, resource_id, expires_at, webhook_url)
            VALUES (%s, %s, %s, to_timestamp(%s::bigint / 1000.0), %s)
        """, (
            store_id,
            data['channel_id'],
            data['resource_id'],
            data['expires_at'],
        data['webhook_url']
        ))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True, 'store_id': store_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/upload-media', methods=['POST'])
def upload_media():
    """
    Compress a product image and upload it to Supabase Storage.

    Expects JSON with: base64_data (raw or data-URL-prefixed base64 image),
    filename, optional queue_id, optional mimeType.
    """
    try:
        payload = request.json
        
        b64_string = payload.get('base64_data', '')
        if "," in b64_string:
            b64_string = b64_string.split(",", 1)[1]
            
        image_data = base64.b64decode(b64_string)
        original_filename = payload.get('filename', 'image.jpg')
        filename_without_ext = os.path.splitext(original_filename)[0]
        queue_id = payload.get('queue_id', '')
        # Namespace the filename by queue_id to prevent images from different
        # products overwriting each other in the shared storage bucket.
        unique_prefix = str(queue_id) if queue_id else os.urandom(4).hex()
        
        original_size_kb = len(image_data) / 1024
        compression_status = "success"
        compression_error = None
        
        try:
            # --- Attempt to compress/normalize the image ---
            with io.BytesIO(image_data) as img_stream:
                with Image.open(img_stream) as image:
                    if image.mode in ("RGBA", "P"):
                        image = image.convert("RGBA")
                    else:
                        image = image.convert("RGB")
                    
                    # Downscale to fit within 1920x1080
                    image.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
                    
                    # Re-encode as WEBP
                    with io.BytesIO() as compressed:
                        image.save(compressed, format='WEBP', quality=80, method=6)
                        image_data = compressed.getvalue()
            
            webp_compressed_kb = len(image_data) / 1024
            content_type = 'image/webp'
            final_filename = f"{unique_prefix}_{filename_without_ext}.webp"
            
        except Exception as e:
            # Compression failed, fall back to
            # uploading the original, uncompressed file instead of erroring out.
            compression_status = "failed"
            compression_error = str(e)
            final_filename = f"{unique_prefix}_{original_filename}"
            content_type = payload.get('mimeType', 'image/jpeg')
        
        # Upload to Supabase Storage
        supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(
            path=final_filename,
            file=image_data,
            file_options={"content-type": content_type, "x-upsert": "true"}
        )
        
        public_url = supabase.storage.from_(SUPABASE_BUCKET_NAME).get_public_url(final_filename)
        
        detail = {
            "original_size_kb": original_size_kb,
            "compression_status": compression_status,
            "compression_error": compression_error
        }
        
        del image_data  
        return jsonify({'success': True, 'urls': [public_url], 'details': [detail]}), 200
        
    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/create-product', methods=['POST'])
def create_product():
    data = request.get_json(force=True, silent=True)
    
    if not data:
        return jsonify({'success': False, 'error': 'Invalid or missing JSON payload'}), 400

    try:
        wc_user, wc_pass = get_store_credentials(data['store_id'])
                        
        if not wc_user or not wc_pass:
            return jsonify({'success': False, 'error': 'Store not found'}), 404          
        
        store_url = WC_BASE_URL
        
        session = requests.Session()
        session.auth = (wc_user, wc_pass)
                        
        base_sku = data.get('folder_name', data.get('sku', '')).strip()
        product_status = data.get('status') or data.get('default_status') or 'draft'
        
        # A product is "variable" (has purchasable variations, e.g. size/color)
        # if it was given a non-empty attributes list; otherwise it's "simple".
        raw_attributes = data.get('attributes', [])
        is_variable = isinstance(raw_attributes, list) and len(raw_attributes) > 0
        product_type = 'variable' if is_variable else 'simple'

        # --- Reshape attributes into WooCommerce's expected format ---
        wc_attributes = []
        if is_variable:
            for attr in raw_attributes:
                opts = attr.get('options', [])
                string_options = [o.get('value', str(o)) if isinstance(o, dict) else str(o) for o in opts]
                wc_attributes.append({
                    'name': attr.get('name'),
                    'visible': True,
                    'variation': True,
                    'options': string_options
                })  
                        
        # Put any image whose name/src contains "main" first, since
        # WooCommerce uses the first image in the list as the featured image.
        raw_images = data.get('images', [])
        sorted_images = sorted(raw_images, key=lambda img: 0 if 'main' in str(img.get('name', '') or img.get('src', '')).lower() else 1)

        # --- Normalize tags
        raw_tags = data.get('tags', [])
        formatted_tags = []
        if isinstance(raw_tags, str):
            for t in raw_tags.split(','):
                if t.strip(): formatted_tags.append({'name': t.strip()})
        elif isinstance(raw_tags, list):
            for t in raw_tags:
                if isinstance(t, dict): 
                    formatted_tags.append(t)
                elif str(t).strip(): 
                    formatted_tags.append({'name': str(t).strip()})

        # --- Normalize categories: accept WooCommerce IDs directly, or resolve
        # plain category names to IDs via the WooCommerce API/cache
        raw_categories = data.get('categories', [])
        formatted_categories = []
        if isinstance(raw_categories, list):
            for cat in raw_categories:
                if isinstance(cat, dict) and 'id' in cat:
                    formatted_categories.append({'id': cat['id']})
                else:
                    cat_name = cat if isinstance(cat, str) else cat.get('name', '')
                    if cat_name:
                        found_cats = get_wc_category(store_url, wc_user, wc_pass, cat_name)
                        if found_cats:
                            formatted_categories.extend(found_cats)

        # --- STEP 1: CHECK IF SKU EXISTS -> SKIP & MARK AS 'exists' ---
        if base_sku:
            existing_res = session.get(f'{store_url}/wp-json/wc/v3/products', params={'sku': base_sku})
            if existing_res.ok and existing_res.json():
                return jsonify({
                    'success': True,
                    'skipped': True,
                    'status': 'exists',
                    'reason': f'SKU already exists ({base_sku}).'
                }), 200

        # --- STEP 2: CREATE THE ENTIRE PRODUCT AT ONCE (WITHOUT SKU) ---
        full_payload_no_sku = {
            'name': data['title'],
            'type': product_type,
            'status': product_status,
            'description': data.get('description', ''),
            'short_description': data.get('short_description', ''),
            'stock_status': 'instock',
            'manage_stock': False,
            'slug': data.get('permalink', ''),
            'categories': formatted_categories,   
            'tags': formatted_tags,
            'attributes': wc_attributes,
            'images': sorted_images
        }
        
        if not is_variable:
            full_payload_no_sku['regular_price'] = str(data.get('price') or '100')

        create_res = session.post(
            f'{store_url}/wp-json/wc/v3/products',
            json=full_payload_no_sku
        )

        if not create_res.ok:
            return jsonify({'success': False, 'error': f"Failed to create product: {create_res.text}"}), create_res.status_code
        
        result = create_res.json()
        product_id = result.get('id')

        # --- STEP 3: ATTACH SKU AFTER CREATION (GHOST SKU LOGIC FALLBACK) ---
        if base_sku and product_id:
            sku_res = session.put(
                f'{store_url}/wp-json/wc/v3/products/{product_id}',
                json={'sku': base_sku}
            )
            
            if not sku_res.ok:
                session.delete(f'{store_url}/wp-json/wc/v3/products/{product_id}', params={'force': 'true'})
                return jsonify({'success': True, 'skipped': True, 'reason': f'SKU conflict lookup table ({base_sku}). Product deleted.'})
                            
        # --- STEP 4: VARIATIONS---
        if is_variable and product_id:
            normalized_attributes = []
            for attr in raw_attributes:
                attr_name = attr.get('name')
                opts = attr.get('options', [])
                if not attr_name or not isinstance(opts, list):
                    continue

                normalized_options = []
                for option in opts:
                    if isinstance(option, dict):
                        option_val = str(option.get('value', '')).strip()
                        price_val = option.get('price', '')
                    else:
                        option_val = str(option).strip()
                        price_val = ''

                    if option_val:
                        normalized_options.append({
                            'name': attr_name,
                            'option': option_val,
                            'price': price_val
                        })

                if normalized_options:
                    normalized_attributes.append(normalized_options)

            if normalized_attributes:
                def create_variation(combo):
                    """
                    Create a single WooCommerce variation for one specific
                    combination of attribute options (e.g. Size=M, Color=Red).
                    Uses the first option-specific price found in the combo,
                    falling back to the product-level price.
                    """
                    combo_prices = [item.get('price') for item in combo if item.get('price') not in (None, '')]
                    final_price = combo_prices[0] if combo_prices else data.get('price', '')

                    variation_payload = {
                        'regular_price': str(final_price or '100'),
                        'status': 'publish',
                        'stock_status': 'instock',
                        'manage_stock': False,
                        'attributes': [
                            {
                                'name': item['name'],
                                'option': item['option']
                            }
                            for item in combo
                        ]
                    }

                    return session.post(
                        f'{store_url}/wp-json/wc/v3/products/{product_id}/variations',
                        json=variation_payload,
                        timeout=60
                    )
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                    executor.map(create_variation, itertools.product(*normalized_attributes))
        
        return jsonify({
            'success': True,
            'product_id': product_id,
            'product_url': result.get('permalink')
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/extract-text', methods=['POST'])
def extract_text():
    """
    Download a file from Google Drive and extract its plain text content.
    Supports two source types:
      - Native Google Docs.
      - Uploaded .docx files.

    Any other mime_type is rejected as unsupported.
    """
    try:
        data = request.json or {}
        file_id = data.get('file_id')
        mime_type = data.get('mime_type')

        if not file_id or not mime_type:
            return jsonify({"success": False, "error": "Missing file_id or mime_type"}), 400

        service = get_drive_service()
        file_stream = io.BytesIO()

        if mime_type == 'application/vnd.google-apps.document':
            # Native Google Doc
            request_api = service.files().export_media(fileId=file_id, mimeType='text/plain')
            downloader = MediaIoBaseDownload(file_stream, request_api)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            extracted_text = file_stream.getvalue().decode('utf-8')

        elif mime_type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
            # Uploaded .docx file
            request_api = service.files().get_media(fileId=file_id)
            downloader = MediaIoBaseDownload(file_stream, request_api)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            
            file_stream.seek(0)
            doc = Document(file_stream)
            extracted_text = '\n'.join([paragraph.text for paragraph in doc.paragraphs])
        
        else:
            return jsonify({"success": False, "error": f"Unsupported mimeType: {mime_type}"}), 400

        return jsonify({"success": True, "text": extracted_text})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001)
