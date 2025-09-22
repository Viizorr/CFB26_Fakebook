import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from functools import wraps

from flask import Flask, render_template, redirect, url_for, flash, request, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, text
from sqlalchemy.exc import OperationalError, InterfaceError
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_migrate import Migrate

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

from decimal import Decimal, InvalidOperation

# ---- odds/math helpers ----
def american_to_decimal(odds) -> Decimal:
    try:
        o = Decimal(str(odds))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("1")
    if o == 0:
        return Decimal("1")
    return (o/Decimal("100") + 1) if o > 0 else (Decimal("100")/(-o) + 1)

def _dec(x):
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return None

def _grade_ou(actual: Decimal, line: Decimal, pick: str):
    """Return 'won'|'lost'|'push' (or None if insufficient info)."""
    if actual is None or line is None:
        return None
    if actual == line:
        return "push"
    p = (pick or "").upper()
    if p == "OVER":
        return "won" if actual > line else "lost"
    if p == "UNDER":
        return "won" if actual < line else "lost"
    return None

def _product(nums):
    out = Decimal("1")
    for n in nums:
        out *= n
    return out

# ---- settle all parlays that had at least one leg in this game ----
def settle_parlays_for_game(game_id):
    # Find candidate parlays
    affected_parlay_ids = (
        db.session.query(Bet.parlay_id)
        .filter(Bet.game_id == game_id, Bet.parlay_id.isnot(None))
        .distinct()
        .all()
    )
    affected_parlay_ids = [pid for (pid,) in affected_parlay_ids]
    if not affected_parlay_ids:
        return

    parlays = Parlay.query.filter(
        Parlay.id.in_(affected_parlay_ids),
        Parlay.status == "pending"
    ).all()

    for parlay in parlays:
        legs = Bet.query.filter_by(parlay_id=parlay.id).all()
        if not legs:
            continue

        terminal = {"won", "lost", "push", "void"}
        if any(l.status not in terminal for l in legs):
            continue  # still waiting on a leg

        if any(l.status == "lost" for l in legs):
            parlay.status = "lost"
            parlay.payout = Decimal("0.00")
        else:
            # Build combined decimal odds from WON legs; PUSH/VOID = 1.0
            multipliers = [american_to_decimal(l.odds) if l.status == "won" else Decimal("1") for l in legs]
            combined = _product(multipliers)

            if any(l.status == "won" for l in legs):
                parlay.status = "won"
                parlay.payout = (parlay.stake * combined).quantize(Decimal("0.01"))
            else:
                # all legs push/void => refund
                parlay.status = "push"
                parlay.payout = parlay.stake

        if parlay.status in ("won", "push"):
            parlay.user.balance += parlay.payout

    db.session.commit()

# ------------------------- DB URL & App Setup -------------------------

db_path = os.getenv("DATABASE_URL")

if db_path and db_path.startswith("postgres://"):
    db_path = db_path.replace("postgres://", "postgresql://", 1)

# If DATABASE_URL is not set, fall back to local SQLite
if not db_path:
    db_path = f"sqlite:///{os.path.join(BASE_DIR, 'app.db')}"

app = Flask(__name__)
print(f"--- Configuring database with URL: {db_path} ---")
app.config.update(
    SECRET_KEY=os.getenv('SECRET_KEY', 'a_default_secret_key_for_dev'),
    SQLALCHEMY_DATABASE_URI=db_path,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
)

db = SQLAlchemy(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


@app.before_request
def keep_db_alive():
    """Ping DB each request; if the pooled connection is stale, reset the session."""
    try:
        db.session.execute(text('SELECT 1'))
    except (OperationalError, InterfaceError):
        db.session.remove()  # drop the bad connection; next use will reconnect

# ----------------------------- Models --------------------------------

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    balance = db.Column(db.Numeric(12, 2), default=Decimal("1000.00"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    bets = db.relationship("Bet", cascade="all, delete-orphan")
    parlay_bets = db.relationship("ParlayBet", cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


game_tag = db.Table(
    "game_tag",
    db.Column("game_id", db.Integer, db.ForeignKey("game.id"), primary_key=True),
    db.Column("tag_id", db.Integer, db.ForeignKey("tag.id"), primary_key=True),
)

class LeagueInfo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(40), unique=True, nullable=False)

class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    home_team = db.Column(db.String(80), nullable=False)
    away_team = db.Column(db.String(80), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default="open", nullable=False)

    ml_home = db.Column(db.Integer, nullable=True)
    ml_away = db.Column(db.Integer, nullable=True)

    spread_line = db.Column(db.Numeric(5, 2), nullable=True)
    spread_home_odds = db.Column(db.Integer, nullable=True)
    spread_away_odds = db.Column(db.Integer, nullable=True)

    total_points = db.Column(db.Numeric(5, 2), nullable=True)
    over_odds = db.Column(db.Integer, nullable=True)
    under_odds = db.Column(db.Integer, nullable=True)

    home_score = db.Column(db.Integer, nullable=True)
    away_score = db.Column(db.Integer, nullable=True)
    
    tags = db.relationship("Tag", secondary=game_tag, backref="games")
    props = db.relationship("Prop", backref="game", cascade="all, delete-orphan")


class Bet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    game_id = db.Column(db.Integer, db.ForeignKey("game.id"), nullable=False)
    prop_id = db.Column(db.Integer, db.ForeignKey("prop.id"), nullable=True)

    bet_type = db.Column(db.String(10), nullable=False)
    selection = db.Column(db.String(10), nullable=False)
    odds = db.Column(db.Integer, nullable=False)
    line = db.Column(db.Numeric(5, 2), nullable=True)

    stake = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(10), default="pending", nullable=False)
    payout = db.Column(db.Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User")
    game = db.relationship("Game")
    
    __table_args__ = (CheckConstraint("stake > 0", name="ck_bet_positive_stake"),)


class ParlayBet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    stake = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(10), default="pending", nullable=False)
    payout = db.Column(db.Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User")
    legs = db.relationship("ParlayLeg", backref="parlay", cascade="all, delete-orphan")


class ParlayLeg(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    parlay_id = db.Column(db.Integer, db.ForeignKey("parlay_bet.id"), nullable=False)
    game_id = db.Column(db.Integer, db.ForeignKey("game.id"), nullable=False)

    bet_type = db.Column(db.String(10), nullable=False)
    selection = db.Column(db.String(10), nullable=False)
    odds = db.Column(db.Integer, nullable=False)
    line = db.Column(db.Numeric(5, 2), nullable=True)
    result = db.Column(db.String(10), default="pending", nullable=False)
    game = db.relationship("Game")


class Prop(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("game.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    prop_type = db.Column(db.String(8), nullable=False) # 'OU' or 'YN'

    # For OU props
    line = db.Column(db.Numeric(7, 2), nullable=True)
    over_odds = db.Column(db.Integer, nullable=True)
    under_odds = db.Column(db.Integer, nullable=True)

    # For Yes/No props
    yes_odds = db.Column(db.Integer, nullable=True)
    no_odds = db.Column(db.Integer, nullable=True)

    status = db.Column(db.String(12), default="open", nullable=False)
    result_value = db.Column(db.Numeric(7, 2), nullable=True)
    result_bool = db.Column(db.Boolean, nullable=True)

# ---------------------------- Helpers --------------------------------

@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (OperationalError, InterfaceError):
        db.session.remove()
        return db.session.get(User, int(user_id))

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper

def american_profit(stake: Decimal, odds: int) -> Decimal:
    stake = Decimal(stake)
    if odds > 0:
        return (stake * Decimal(odds) / Decimal(100)).quantize(Decimal("0.01"))
    else:
        return (stake * Decimal(100) / Decimal(abs(odds))).quantize(Decimal("0.01"))

# --- ADDED THIS HELPER FUNCTION ---
def get_or_create_tag(name: str) -> Tag:
    name = name.strip()
    if not name:
        return None
    t = Tag.query.filter_by(name=name).first()
    if not t:
        t = Tag(name=name)
        db.session.add(t)
    return t
# ----------------------------------

def to_decimal(value):
    """Safely converts a form value to a Decimal, returning None if invalid."""
    if value is None or str(value).strip() == '':
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None

def to_int(value):
    """Safely converts a form value to an integer, returning None if invalid."""
    if value is None or str(value).strip() == '':
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None

# ----------------------------- Routes --------------------------------

@app.route("/healthz")
def healthz():
    return "ok", 200

# --- UPDATED THIS ROUTE ---
@app.route('/')
@login_required
def index():
    tag_name = request.args.get('tag', '').strip()
    query = Game.query
    if tag_name:
        query = query.join(Game.tags).filter(Tag.name == tag_name)
    
    open_games = query.filter(Game.status == 'open').order_by(Game.start_time.asc()).all()
    past_games = query.filter(Game.status != 'open').order_by(Game.start_time.desc()).all()
    
    all_tags = Tag.query.order_by(Tag.name.asc()).all()
    return render_template('index.html', open_games=open_games, past_games=past_games, all_tags=all_tags, current_tag=tag_name)
# ---------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        user = User.query.filter_by(username=u).first()
        if user and user.check_password(p):
            login_user(user)
            return redirect(url_for("index"))
        flash("Invalid credentials", "danger")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        if not u or not p:
            flash("Username and password required", "danger")
            return render_template("register.html")
        if User.query.filter_by(username=u).first():
            flash("Username already taken", "warning")
            return render_template("register.html")
        user = User(username=u)
        user.set_password(p)
        db.session.add(user)
        db.session.commit()
        flash("Registered! Please login.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/game/<int:game_id>")
@login_required
def game_detail(game_id):
    game = Game.query.get_or_404(game_id)
    return render_template("game_detail.html", game=game)

@app.route('/bet', methods=['POST'])
@login_required
def place_bet():
    stake = to_decimal(request.form.get('stake'))
    bets_json = request.form.get('bets')

    if not stake or stake <= 0 or not bets_json:
        flash('Invalid bet information provided.', 'danger')
        return redirect(request.referrer or url_for('index'))

    if current_user.balance < stake:
        flash('Insufficient balance.', 'danger')
        return redirect(url_for('account'))
    
    try:
        bets_data = json.loads(bets_json)
        if not isinstance(bets_data, list) or not bets_data:
            raise ValueError("Bets data is empty or not a list.")
    except (json.JSONDecodeError, ValueError) as e:
        flash(f'Error processing bet data: {e}', 'danger')
        return redirect(request.referrer or url_for('index'))

    current_user.balance -= stake

    # --- PARLAY LOGIC ---
    if len(bets_data) > 1:
        parlay = ParlayBet(user_id=current_user.id, stake=stake)
        db.session.add(parlay)
        
        for leg_data in bets_data:
            leg = ParlayLeg(
                parlay=parlay,
                game_id=to_int(leg_data.get('gameId')),
                bet_type=leg_data.get('betType'),
                selection=leg_data.get('selection'),
                odds=to_int(leg_data.get('price')),
                line=to_decimal(leg_data.get('line'))
            )
            db.session.add(leg)
        flash('Parlay bet placed successfully!', 'success')

    # --- SINGLE BET LOGIC ---
    else:
        bet_data = bets_data[0]
        bet = Bet(
            user_id=current_user.id,
            game_id=to_int(bet_data.get('gameId')),
            prop_id=to_int(bet_data.get('propId')),
            bet_type=bet_data.get('betType'),
            selection=bet_data.get('selection'),
            odds=to_int(bet_data.get('price')),
            line=to_decimal(bet_data.get('line')),
            stake=stake
        )
        db.session.add(bet)
        flash('Bet placed successfully!', 'success')

    db.session.commit()
    return redirect(url_for('account'))


@app.route("/account")
@login_required
def account():
    bets = Bet.query.filter_by(user_id=current_user.id).order_by(Bet.created_at.desc()).all()
    parlays = ParlayBet.query.filter_by(user_id=current_user.id).order_by(ParlayBet.created_at.desc()).all()
    return render_template("account.html", bets=bets, parlays=parlays)

@app.route("/leaderboard")
@login_required
def leaderboard():
    users = User.query.order_by(User.balance.desc()).all()
    return render_template("leaderboard.html", users=users)

@app.route('/league-info')
def league_info():
    # Get the most recently updated piece of content
    info = LeagueInfo.query.order_by(LeagueInfo.updated_at.desc()).first()
    return render_template('league_info.html', info=info)

@app.route('/admin/league-info/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_edit_league_info():
    info = LeagueInfo.query.first() # Get the first entry to edit it
    if request.method == 'POST':
        content = request.form.get('content')
        if info:
            # If info already exists, update it
            info.content = content
        else:
            # If this is the first time, create a new entry
            info = LeagueInfo(content=content)
            db.session.add(info)
        db.session.commit()
        flash('League Info updated successfully!', 'success')
        return redirect(url_for('league_info'))

    return render_template('admin_edit_info.html', info=info)

# ------------------------------ Admin --------------------------------

@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin_users.html', users=users)

@app.route('/admin/users/create', methods=['POST'])
@login_required
@admin_required
def admin_create_user():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    balance = to_decimal(request.form.get('start_balance', '1000.00'))
    is_admin = request.form.get('is_admin') == '1'

    if not username or not password:
        flash('Username and password are required.', 'danger')
        return redirect(url_for('admin_users'))

    if User.query.filter_by(username=username).first():
        flash('Username already exists.', 'warning')
        return redirect(url_for('admin_users'))

    new_user = User(username=username, balance=balance, is_admin=is_admin)
    new_user.set_password(password)
    
    db.session.add(new_user)
    db.session.commit()
    flash(f'User {username} created successfully.', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/adjust_balance', methods=['POST'])
@login_required
@admin_required
def admin_adjust_balance(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin_users'))

    amount = to_decimal(request.form.get('amount'))
    if amount is None:
        flash('Invalid amount entered.', 'danger')
        return redirect(url_for('admin_users'))

    user.balance += amount
    db.session.commit()
    flash(f"Balance for {user.username} updated successfully.", 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_user(user_id):
    if user_id == current_user.id:
        flash('You cannot delete your own account.', 'danger')
        return redirect(url_for('admin_users'))
    
    user_to_delete = db.session.get(User, user_id)
    if user_to_delete:
        db.session.delete(user_to_delete)
        db.session.commit()
        flash(f'User {user_to_delete.username} has been deleted.', 'success')
    else:
        flash('User not found.', 'danger')
    
    return redirect(url_for('admin_users'))

@app.route('/admin/games')
@login_required
@admin_required
def admin_games():
    games = Game.query.order_by(Game.start_time.desc()).all()
    return render_template('admin_games.html', games=games)

# --- UPDATED THIS ROUTE ---
@app.route('/admin/games/new', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_new_game():
    if request.method == 'POST':
        home_team = request.form.get('home_team', '').strip()
        away_team = request.form.get('away_team', '').strip()
        start_time_str = request.form.get('start_time')

        if not all([home_team, away_team, start_time_str]):
            flash('Home team, away team, and start time are required.', 'danger')
            return render_template('admin_edit_game.html', game=None)
        
        try:
            start_time = datetime.fromisoformat(start_time_str)
        except ValueError:
            flash('Invalid start time format.', 'danger')
            return render_template('admin_edit_game.html', game=None)

        game = Game(
            home_team=home_team,
            away_team=away_team,
            start_time=start_time,
            status='open',
            ml_home=to_int(request.form.get('ml_home')),
            ml_away=to_int(request.form.get('ml_away')),
            spread_line=to_decimal(request.form.get('spread_line')),
            spread_home_odds=to_int(request.form.get('spread_home_odds')),
            spread_away_odds=to_int(request.form.get('spread_away_odds')),
            total_points=to_decimal(request.form.get('total_points')),
            over_odds=to_int(request.form.get('over_odds')),
            under_odds=to_int(request.form.get('under_odds')),
        )

        raw_tags = request.form.get('tags', '')
        tag_names = [name.strip() for name in raw_tags.split(',') if name.strip()]
        if tag_names:
            game.tags = [get_or_create_tag(name) for name in tag_names]
        
        db.session.add(game)
        db.session.commit()
        flash('Game created successfully.', 'success')
        return redirect(url_for('admin_games'))

    return render_template('admin_edit_game.html', game=None)
# ---------------------------

# --- UPDATED THIS ROUTE ---
@app.route('/admin/games/<int:game_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_edit_game(game_id):
    game = Game.query.get_or_404(game_id)
    if request.method == 'POST':
        game.home_team = request.form.get('home_team', game.home_team).strip()
        game.away_team = request.form.get('away_team', game.away_team).strip()
        game.status = request.form.get('status', game.status)

        if request.form.get('start_time'):
            try:
                game.start_time = datetime.fromisoformat(request.form.get('start_time'))
            except ValueError:
                flash('Invalid date format.', 'danger')
                return render_template('admin_edit_game.html', game=game)
        
        game.ml_home = to_int(request.form.get('ml_home'))
        game.ml_away = to_int(request.form.get('ml_away'))
        game.spread_line = to_decimal(request.form.get('spread_line'))
        game.spread_home_odds = to_int(request.form.get('spread_home_odds'))
        game.spread_away_odds = to_int(request.form.get('spread_away_odds'))
        game.total_points = to_decimal(request.form.get('total_points'))
        game.over_odds = to_int(request.form.get('over_odds'))
        game.under_odds = to_int(request.form.get('under_odds'))
        
        raw_tags = request.form.get('tags', '')
        tag_names = [name.strip() for name in raw_tags.split(',') if name.strip()]
        game.tags = [get_or_create_tag(name) for name in tag_names if name]

        db.session.commit()
        flash('Game updated successfully.', 'success')
        return redirect(url_for('admin_games'))

    return render_template('admin_edit_game.html', game=game)
# ---------------------------

@app.route('/admin/games/<int:game_id>/close', methods=['POST'])
@login_required
@admin_required
def admin_close_game(game_id):
    game = Game.query.get_or_404(game_id)
    game.status = 'closed'
    db.session.commit()
    flash('Betting closed for game.', 'info')
    return redirect(url_for('admin_games'))

@app.route('/admin/games/<int:game_id>/reopen', methods=['POST'])
@login_required
@admin_required
def admin_reopen_game(game_id):
    game = Game.query.get_or_404(game_id)
    game.status = 'open'
    db.session.commit()
    flash('Betting reopened for game.', 'info')
    return redirect(url_for('admin_games'))

@app.route('/admin/games/<int:game_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_game(game_id):
    game = Game.query.get_or_404(game_id)
    db.session.delete(game)
    db.session.commit()
    flash('Game deleted.', 'warning')
    return redirect(url_for('admin_games'))

from decimal import Decimal

@app.route("/admin/games/<int:game_id>/grade", methods=["POST"])
@login_required
@admin_required
def admin_grade_game(game_id):
    game = Game.query.get_or_404(game_id)

    # --- tiny local helpers (safe numeric + OU grading) ---
    from decimal import Decimal, InvalidOperation
    def _dec(x):
        if x is None:
            return None
        try:
            return Decimal(str(x))
        except (InvalidOperation, TypeError, ValueError):
            return None

    def _grade_ou(actual: Decimal, line: Decimal, pick: str):
        """Return 'won'|'lost'|'push' or None if insufficient info."""
        if actual is None or line is None:
            return None
        if actual == line:
            return "push"
        p = (pick or "").upper()
        if p == "OVER":
            return "won" if actual > line else "lost"
        if p == "UNDER":
            return "won" if actual < line else "lost"
        return None

    # --- parse & persist game score ---
    home_score = to_int(request.form.get("home_score"))
    away_score = to_int(request.form.get("away_score"))
    if home_score is None or away_score is None:
        flash("Invalid scores provided.", "danger")
        return redirect(url_for("admin_games"))

    game.home_score = home_score
    game.away_score = away_score

    total_score = home_score + away_score
    margin = home_score - away_score  # home - away

    # --- grade all pending single legs for this game ---
    pending_bets = Bet.query.filter_by(game_id=game.id, status="pending").all()

    for bet in pending_bets:
        result = "lost"
        payout = Decimal("0.00")

        bt   = (bet.bet_type or "").upper()
        pick = (bet.selection or "").upper()
        line = _dec(getattr(bet, "line", None))
        stake = _dec(bet.stake) or Decimal("0.00")

        # MONEYLINE
        if bt == "ML":
            winner = "HOME" if margin > 0 else ("AWAY" if margin < 0 else None)
            if winner is None:
                result, payout = "push", stake
            elif pick == winner:
                result = "won"
                payout = stake + american_profit(stake, bet.odds)

        # SPREAD (assumes line references HOME side, e.g., HOME -3.5)
        elif bt == "SPREAD":
            if line is None:
                result, payout = "void", stake
            else:
                if pick == "HOME":
                    adj = Decimal(margin) - line
                elif pick == "AWAY":
                    # equivalent to: away_margin - away_line
                    adj = Decimal(-margin) - Decimal(-line)
                else:
                    adj = None
                    result, payout = "void", stake

                if adj is not None:
                    if adj == 0:
                        result, payout = "push", stake
                    elif adj > 0:
                        result = "won"
                        payout = stake + american_profit(stake, bet.odds)
                    else:
                        result, payout = "lost", Decimal("0.00")

        # GAME TOTAL
        elif bt in ("TOTAL", "TOTALS", "OU", "O/U"):
            if line is None:
                result, payout = "void", stake
            else:
                ou = _grade_ou(_dec(total_score), line, pick)
                if ou is not None:
                    result = ou
                    if result == "won":
                        payout = stake + american_profit(stake, bet.odds)
                    elif result == "push":
                        payout = stake

        # PROP (OVER/UNDER against a numeric actual you submit as prop_value_<bet.id>)
        elif bt == "PROP":
            actual = _dec(request.form.get(f"prop_value_{bet.id}"))
            if actual is None or line is None:
                # can't grade yet; leave this bet pending so you can resubmit with actual
                continue
            ou = _grade_ou(actual, line, pick)
            if ou is not None:
                result = ou
                if result == "won":
                    payout = stake + american_profit(stake, bet.odds)
                elif result == "push":
                    payout = stake

        # Unknown bet type -> safest is void (refund)
        else:
            result, payout = "void", stake

        # persist leg result
        bet.status = result
        bet.payout = payout

        # Credit only non-parlay bets here; parlay legs get paid at parlay level
        if bet.parlay_id is None and result in ("won", "push", "void"):
            bet.user.balance += payout

    # Save leg results so the parlay settle can see them
    db.session.flush()

    # Settle parlays that included this game (requires helper defined above your routes)
    try:
        settle_parlays_for_game(game.id)
    except NameError:
        # If you haven't pasted the helper yet, skip gracefully.
        pass

    game.status = "graded"
    db.session.commit()
    flash("Game graded and balances updated.", "success")
    return redirect(url_for("admin_games"))

@app.route('/admin/games/<int:game_id>/props/new', methods=['POST'])
@login_required
@admin_required
def admin_prop_new(game_id):
    game = Game.query.get_or_404(game_id)
    name = request.form.get('name', '').strip()
    prop_type = request.form.get('prop_type', 'OU').strip()

    if not name:
        flash('Prop name is required.', 'danger')
        return redirect(url_for('admin_edit_game', game_id=game.id))

    prop = Prop(game_id=game.id, name=name, prop_type=prop_type, status='open')
    if prop_type == 'OU':
        prop.line = to_decimal(request.form.get('line'))
        prop.over_odds = to_int(request.form.get('over_odds'))
        prop.under_odds = to_int(request.form.get('under_odds'))
    else: # YN
        prop.yes_odds = to_int(request.form.get('yes_odds'))
        prop.no_odds = to_int(request.form.get('no_odds'))

    db.session.add(prop)
    db.session.commit()
    flash('Prop created.', 'success')
    return redirect(url_for('admin_edit_game', game_id=game.id))

@app.route('/admin/props/<int:prop_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_prop(prop_id):
    prop_to_delete = db.session.get(Prop, prop_id)
    if prop_to_delete:
        game_id = prop_to_delete.game_id # Get game_id before deleting
        db.session.delete(prop_to_delete)
        db.session.commit()
        flash(f'Prop ID {prop_id} has been deleted.', 'success')
        return redirect(url_for('admin_edit_game', game_id=game_id))
    else:
        flash('Prop not found.', 'danger')
        return redirect(url_for('admin_games'))

@app.route('/admin/props/<int:prop_id>/status', methods=['POST'])
@login_required
@admin_required
def admin_prop_status(prop_id):
    prop = Prop.query.get_or_404(prop_id)
    new_status = request.form.get('status', '').strip()
    if new_status in ('open', 'closed'):
        prop.status = new_status
        db.session.commit()
        flash(f'Prop status set to {new_status}.', 'info')
    else:
        flash('Invalid status provided.', 'danger')
    return redirect(url_for('admin_edit_game', game_id=prop.game_id))

@app.route('/admin/props/<int:prop_id>/grade', methods=['POST'])
@login_required
@admin_required
def admin_grade_prop(prop_id):
    prop = Prop.query.get_or_404(prop_id)
    if prop.prop_type == 'OU':
        result_value = to_decimal(request.form.get('result_value'))
        if result_value is None:
            flash('A valid decimal result is required.', 'danger')
        else:
            prop.result_value = result_value
            prop.status = 'graded'
            flash('Prop graded successfully.', 'success')
    else: # YN
        result_bool_str = request.form.get('result_bool', '').lower()
        if result_bool_str not in ('true', 'false'):
            flash('A valid result (true/false) is required.', 'danger')
        else:
            prop.result_bool = (result_bool_str == 'true')
            prop.status = 'graded'
            flash('Prop graded successfully.', 'success')
    
    db.session.commit()
    # Note: Grading bets on props would happen here or in a separate batch job.
    return redirect(url_for('admin_edit_game', game_id=prop.game_id))


if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5000)
