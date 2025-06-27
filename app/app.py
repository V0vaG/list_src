from flask import Flask, render_template, redirect, request, url_for, flash, session, jsonify
from flask import send_from_directory
import json
import os
import requests
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from flask import g
import uuid
from datetime import datetime
from werkzeug.utils import secure_filename
from zipfile import ZipFile
import io
from flask import send_file
from collections import defaultdict
import unicodedata

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key'
app_version = os.getenv('VERSION', '0.0.0')

# Set up paths
alias = "list"
HOME_DIR = os.path.expanduser("~")
FILES_PATH = os.path.join(HOME_DIR, "script_files", alias)
DATA_DIR = os.path.join(FILES_PATH, "data")
ITEMS_FILE = os.path.join(DATA_DIR, 'items.json')
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


USERS_FILE = os.path.join(DATA_DIR, 'users.json')
LISTS_FILE = os.path.join(DATA_DIR, 'shopping_lists.json')

CATEGORY_MAP = {
    "אוכל": ["פירות", "ירקות", "בשרי", "קטניות", "חלבי", "שתייה", "קפואים", "חטיפים", "דגים", "תבלינים", "מאפים", "דגנים", "ממתקים", "אחר"],
    "סופר-פארם": ["רפואי", "לילדים", "הגיינה", "טיפוח", "בריאות", "מוצרי ניקוי"],
    "ניקיון": ["למטבח", "לשירותים","לאמבטיה", "אחר"],
    "לבית": ["ריהוט", "כלים", "חשמל", "טקסטיל", "אחסון", "תאורה", "אחר"],
    "רכב": ["תחזוקה", "אביזרים", "דלק", "ניקוי", "בטיחות", "אחר"],
    "אחר": ["*"]
}

# Ensure the directory exists
os.makedirs(DATA_DIR, exist_ok=True)

# ------------------ Helpers ------------------

def ensure_edit_permission_field():
    users = load_users()
    modified = False
    if len(users) > 1 and "users" in users[1]:
        for user in users[1]["users"]:
            if "edit_item_list" not in user:
                user["edit_item_list"] = False
                modified = True
    if modified:
        save_users(users)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please log in first.", "danger")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/view_all_items')
@login_required
def view_all_items():
    if not session.get('is_root'):
        username = session.get('user_id')
        users = load_users()
        user_data = next((u for u in users[1]["users"] if u["username"] == username), None)
        if not user_data or not user_data.get("edit_item_list", False):
            flash("Access denied", "danger")
            return redirect(url_for('user_dashboard'))

    all_items = load_items()

    # Deduplicate by (name, category.type, category.subtype)
    seen = set()
    unique_items = []
    for item in all_items:
        key = (item['name'], item['category']['type'], item['category']['subtype'])
        if key not in seen:
            seen.add(key)
            # Add full image path
            if 'photo' in item and item['photo']:
                item['photo'] = url_for('uploaded_file', filename=item['photo'])
            unique_items.append(item)

    # Extract unique categories and subtypes for dropdowns
    all_categories = sorted({item['category']['type'] for item in unique_items if 'category' in item})
    all_subtypes = sorted({item['category']['subtype'] for item in unique_items if 'category' in item})

    return render_template(
        'item_list.html',
        items=unique_items,
        category_map=CATEGORY_MAP
    )

@app.route('/export_items')
@login_required
def export_items():
    items = load_items()

    # Create ZIP in memory
    memory_file = io.BytesIO()
    with ZipFile(memory_file, 'w') as zf:
        # Add items.json
        zf.writestr("items.json", json.dumps(items, indent=2))

        # Add images
        for item in items:
            if 'photo' in item and item['photo']:
                image_path = os.path.join(app.config['UPLOAD_FOLDER'], item['photo'])
                if os.path.exists(image_path):
                    zf.write(image_path, arcname=f"images/{item['photo']}")

    memory_file.seek(0)

    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name='items_export.zip'
    )

@app.route('/import_items', methods=['POST'])
@login_required
def import_items():
    uploaded_file = request.files.get('zipfile')
    if not uploaded_file or not uploaded_file.filename.endswith('.zip'):
        flash("Invalid ZIP file.", "danger")
        return redirect(url_for('view_all_items'))

    with ZipFile(uploaded_file) as zf:
        # Extract items.json
        if 'items.json' not in zf.namelist():
            flash("Missing items.json in the ZIP.", "danger")
            return redirect(url_for('view_all_items'))

        items_data = json.loads(zf.read('items.json').decode('utf-8'))

        # Save images
        for item in items_data:
            if 'photo' in item and item['photo']:
                image_name = item['photo']
                image_path = os.path.join(app.config['UPLOAD_FOLDER'], image_name)
                if f"images/{image_name}" in zf.namelist():
                    with open(image_path, 'wb') as f:
                        f.write(zf.read(f"images/{image_name}"))

        save_items(items_data)
        flash("Items imported successfully!", "success")

    return redirect(url_for('view_all_items'))

def load_items():
    if os.path.exists(ITEMS_FILE):
        with open(ITEMS_FILE, 'r') as f:
            items = json.load(f)

        # Deduplicate
        seen = set()
        unique = []
        for item in items:
            key = (item['name'], item['category']['type'], item['category']['subtype'])
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    return []

def save_items(items):
    with open(ITEMS_FILE, 'w') as f:
        json.dump(items, f, indent=4)

@app.route('/item/edit/<item_id>', methods=['POST'])
@login_required
def edit_item(item_id):
    name = request.form.get('name')
    category = request.form.get('category')
    subcategory = request.form.get('subcategory')
    photo_file = request.files.get('photo')

    items = load_items()
    for item in items:
        if item['id'] == item_id:
            item['name'] = name
            item['category']['type'] = category
            item['category']['subtype'] = subcategory

            if photo_file and photo_file.filename:
                ext = os.path.splitext(photo_file.filename)[1]
                photo_filename = f"{item_id}{ext}"
                photo_path = os.path.join(app.config['UPLOAD_FOLDER'], photo_filename)
                photo_file.save(photo_path)
                item['photo'] = photo_filename
            break

    save_items(items)
    flash('Item updated!', 'success')
    return redirect(url_for('view_all_items'))

@app.route('/list/<list_id>/rename', methods=['POST'])
@login_required
def rename_list(list_id):
    new_name = request.form.get('list_name', '').strip()
    lists = load_lists()
    for lst in lists:
        if lst['id'] == list_id:
            lst['name'] = new_name or "Unnamed"
            break
    save_lists(lists)
    flash("List name updated!", "success")
    return redirect(url_for('view_list_shopping', list_id=list_id))

@app.route('/choose_item/<list_id>', methods=['GET', 'POST'])
@login_required
def choose_item(list_id):
    if request.method == 'POST':
        item_id = request.form.get('item_id')
        amount = int(request.form.get('amount', 1))
        unit = request.form.get('unit', 'amount')  # <-- get unit from form, default to 'amount'

        items = load_items()
        selected_item = next((item for item in items if item['id'] == item_id), None)

        if selected_item:
            item_copy = selected_item.copy()
            item_copy['id'] = str(uuid.uuid4())
            item_copy['amount'] = amount
            item_copy['unit'] = unit  # <-- store unit in the item

            lists = load_lists()
            for lst in lists:
                if lst['id'] == list_id:
                    lst['items'].append(item_copy)
                    break
            save_lists(lists)

        return redirect(url_for('view_list_shopping', list_id=list_id))

    items = load_items()
    categories = sorted(set(item['category']['type'] for item in items if 'category' in item))
    return render_template('choose_item.html', items=items, categories=categories, list_id=list_id)

@app.route('/add_item', methods=['GET', 'POST'])
@login_required
def add_item_page():
    if request.method == 'POST':
        name = request.form.get('name')
        category = request.form.get('category')
        subcategory = request.form.get('subcategory')
        photo_file = request.files.get('photo')

        if not name or not photo_file:
            flash("Name and image are required.", "danger")
            return redirect(url_for('add_item_page'))

        item_id = str(uuid.uuid4())
        ext = os.path.splitext(photo_file.filename)[1]
        photo_filename = f"{item_id}{ext}"
        photo_path = os.path.join(app.config['UPLOAD_FOLDER'], photo_filename)
        photo_file.save(photo_path)

        item = {
            "id": item_id,
            "name": name,
            "photo": photo_filename,
            "category": {
                "type": category,
                "subtype": subcategory
            }
        }

        items = load_items()
        items.append(item)
        save_items(items)

        flash("Item added successfully!", "success")
        return redirect(url_for('view_all_items'))

    return render_template('add_item.html')

@app.route('/item/delete/<item_id>', methods=['POST'])
@login_required
def delete_independent_item(item_id):
    items = load_items()
    items = [item for item in items if item['id'] != item_id]
    save_items(items)
    flash("Item deleted.", "info")
    return redirect(url_for('view_all_items'))

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r') as file:
            return json.load(file)
    return []

def save_users(users):
    with open(USERS_FILE, 'w') as file:
        json.dump(users, file, indent=4)

def get_root_user():
    users = load_users()
    return users[0] if users else None

def is_root_registered():
    return bool(get_root_user())

def save_root_user(username, password):
    password_hash = generate_password_hash(password, method='pbkdf2:sha256')
    users = [{"root_user": username, "password_hash": password_hash}, {"users": []}]
    save_users(users)

def save_user(username, password):
    password_hash = generate_password_hash(password, method='pbkdf2:sha256')
    users = load_users()
    users[1]['users'].append({"username": username, "password_hash": password_hash})
    save_users(users)

def remove_user(username):
    users = load_users()
    if len(users) > 1:
        users[1]['users'] = [user for user in users[1]['users'] if user['username'] != username]
        save_users(users)
        return True
    return False

def load_lists():
    if os.path.exists(LISTS_FILE):
        with open(LISTS_FILE, 'r') as f:
            return json.load(f)
    return []

def save_lists(lists):
    with open(LISTS_FILE, 'w') as f:
        json.dump(lists, f, indent=2)

# ------------------ Routes ------------------
@app.before_request
def check_root_user():
    if not is_root_registered():
        if request.endpoint not in ('register_root', 'static'):
            return redirect(url_for('register_root'))

@app.route('/')
def index():
    if not is_root_registered():
        return redirect(url_for('register_root'))
    return redirect(url_for('login'))

@app.route('/register_root', methods=['GET', 'POST'])
def register_root():
    if is_root_registered():
        return redirect(url_for('login'))
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        confirm_password = request.form['confirm_password']
        if password != confirm_password:
            flash('Passwords do not match!', 'danger')
        else:
            save_root_user(username, password)
            flash('Root user registered successfully!', 'success')
            return redirect(url_for('login'))
    return render_template('register_root.html')

@app.route('/register_user', methods=['GET', 'POST'])
def register_user():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        confirm_password = request.form['confirm_password']
        if password != confirm_password:
            flash('Passwords do not match!', 'danger')
        else:
            save_user(username, password)
            flash('User registered successfully!', 'success')
            return redirect(url_for('login'))
    return render_template('register_user.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        users = load_users()
        root_user = users[0] if users else None
        regular_users = users[1]['users'] if len(users) > 1 else []
        if root_user and username == root_user['root_user']:
            if check_password_hash(root_user['password_hash'], password):
                session['user_id'] = username
                session['is_root'] = True
                flash("Logged in as root.", "success")
                return redirect(url_for('user_dashboard'))
        for user in regular_users:
            if user['username'] == username and check_password_hash(user['password_hash'], password):
                session['user_id'] = username
                session['is_root'] = False
                flash("Logged in successfully.", "success")
                return redirect(url_for('user_dashboard'))
        flash("Invalid credentials.", "danger")
    return render_template('login.html', app_version=app_version)

@app.route('/toggle_edit_permission', methods=['POST'])
@login_required
def toggle_edit_permission():
    if not session.get('is_root'):
        flash("Access denied", "danger")
        return redirect(url_for('user_dashboard'))

    username = request.form.get('username')
    users = load_users()
    for user in users[1]["users"]:
        if user["username"] == username:
            user["edit_item_list"] = not user.get("edit_item_list", False)
            break
    save_users(users)
    return redirect(url_for('root_dashboard'))

@app.route('/root_dashboard')
@login_required
def root_dashboard():
    if not session.get('is_root'):
        flash("Access denied", "danger")
        return redirect(url_for('user_dashboard'))
    users_data = load_users()
    user_list = users_data[1]['users'] if len(users_data) > 1 else []
    return render_template('root_dashboard.html', users=user_list)

@app.route('/delete_list', methods=['POST'])
@login_required
def delete_list():
    list_id = request.form['list_id']
    lists = load_lists()
    lists = [lst for lst in lists if lst['id'] != list_id]
    save_lists(lists)
    flash("List deleted successfully.", "success")
    return redirect(url_for('user_dashboard'))

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/remove_user', methods=['POST'])
def remove_user_route():
    username = request.form['username']
    remove_user(username)
    flash('User removed successfully!', 'success')
    return redirect(url_for('root_dashboard'))

@app.route('/user_dashboard')
@login_required
def user_dashboard():
    username = session.get('user_id', 'Unknown')
    role = "root" if session.get('is_root') else "user"
    lists = load_lists()
    user_lists = [lst for lst in lists if lst['created_by'] == username]
    users_data = load_users()
    user_data = {}

    if not session.get('is_root'):
        for u in users_data[1]["users"]:
            if u["username"] == username:
                user_data = u
                break

    return render_template(
        'user_dashboard.html',
        username=username,
        role=role,
        user_lists=user_lists,
        user_data=user_data
    )

@app.route('/list/<list_id>')
@login_required
def view_list_shopping(list_id):
    def normalize(val):
        return unicodedata.normalize("NFKC", str(val).strip()) if val else "לא מוגדר"

    lists = load_lists()
    selected = next((lst for lst in lists if lst['id'] == list_id), None)
    if not selected:
        flash("List not found.", "danger")
        return redirect(url_for("show_lists"))

    grouped = defaultdict(lambda: defaultdict(list))
    for item in selected.get("items", []):
        raw_type = item.get("category", {}).get("type")
        raw_sub = item.get("category", {}).get("subtype")
        cat = normalize(raw_type)
        sub = normalize(raw_sub)

        print(f"📦 GROUPING ITEM: name={item.get('name')} | type={cat} | subtype={sub}")
        grouped[cat][sub].append(item)

    grouped_items = {
        cat: {
            sub: sorted(items, key=lambda i: i.get("name", ""))
            for sub, items in sorted(sub_map.items())
        }
        for cat, sub_map in sorted(grouped.items())
    }

    print(f"✅ FINAL GROUPED CATEGORIES: {list(grouped_items.keys())}")

    return render_template("view_list.html", list_data=selected, grouped_items=grouped_items)

@app.route('/logout')
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/lists')
@login_required
def show_lists():
    user = session.get('user_id')
    lists = load_lists()
    user_lists = [lst for lst in lists if lst['created_by'] == user]
    return render_template('user_dashboard.html', lists=user_lists, user=user)

@app.route('/list/new', methods=['GET', 'POST'])
@login_required
def new_list():
    user = session.get('user_id')

    if request.method == 'POST':
        list_name = request.form.get('list_name', '').strip()
    else:
        list_name = request.args.get('list_name', '').strip()  # support optional list_name via URL param

    new_list_id = str(uuid.uuid4())

    new_list = {
        'id': new_list_id,
        'name': list_name or "Unnamed",
        'created_at': datetime.now().isoformat(),
        'created_by': user,
        'finished_at': None,
        'finished_by': None,
        'items': []
    }

    lists = load_lists()
    lists.append(new_list)
    save_lists(lists)

    session['current_list_id'] = new_list_id
    flash('New list created!', 'success')
    return redirect(url_for('view_list_shopping', list_id=new_list_id))

@app.route('/list/<list_id>/add_item', methods=['POST'])
@login_required
def add_item_to_list(list_id):
    name = request.form['name']
    price = float(request.form['price'])
    category_type = request.form['category_type']
    category_subtype = request.form['category_subtype']
    photo = request.form.get('photo', '')

    item = {
        'id': str(uuid.uuid4()),
        'name': name,
        'photo': photo,
        'price': price,
        'category': {
            'type': category_type,
            'subtype': category_subtype
        }
    }

    lists = load_lists()
    for lst in lists:
        if lst['id'] == list_id:
            lst['items'].append(item)
            lst['total_price'] += price
            break

    save_lists(lists)
    flash('Item added.', 'success')
    return redirect(url_for('view_list_shopping', list_id=list_id))

@app.route('/list/<list_id>/finish', methods=['POST'])
@login_required
def finish_list(list_id):
    user = session.get('user_id')
    lists = load_lists()
    for lst in lists:
        if lst['id'] == list_id:
            lst['finished_at'] = datetime.now().isoformat()
            lst['finished_by'] = user
            break
    save_lists(lists)
    flash('List marked as finished.', 'info')
    return redirect(url_for('user_dashboard'))

@app.route('/item/delete/<list_id>/<item_id>', methods=['POST'])
@login_required
def delete_item(list_id, item_id):
    lists = load_lists()
    for lst in lists:
        if lst['id'] == list_id:
            lst['items'] = [item for item in lst['items'] if item['id'] != item_id]
            break
    save_lists(lists)
    flash('Item deleted.', 'warning')
    return redirect(url_for('view_list_shopping', list_id=list_id))

if __name__ == '__main__':
    app.run(debug=True, threaded=True)
