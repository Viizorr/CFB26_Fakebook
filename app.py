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

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


game_tag = db.Table(
    "game_tag",
    db.Column("game_id", db.Integer, db.ForeignKey("game.id"), primary_key=True),
    db.Column("tag_id", db.Integer, db.ForeignKey("tag.id"), primary_key=True),
)

class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    home_team = db.Column(db.String(80), nullable=False)
    away_team = db.Column(db.String(80), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default="open", nullable=False)  # open, closed, graded

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

    bet_type = db.Column(db.String(10), nullable=False)      # ML, SPREAD, TOTAL
    selection = db.Column(db.String(10), nullable=False)     # HOME/AWAY or OVER/UNDER
    odds = db.Column(db.Integer, nullable=False)
    line = db.Column(db.Numeric(5, 2), nullable=True)

    stake = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(10), default="pending", nullable=False)  # pending, won, lost, push
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


class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(40), unique=True, nullable=False)


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

def get_or_create_tag(name: str) -> Tag:
    name = name.strip()
    if not name:
        return None
    t = Tag.query.filter_by(name=name).first()
    if not t:
        t = Tag(name=name)
        db.session.add(t)
    return t

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
     # ---- START: TEMPORARY DEBUGGING CODE ----
    print("--- FORM DATA RECEIVED ---")
    print(request.form)
    print("--------------------------")
    # ---- END: TEMPORARY DEBUGGING CODE ----
    # This route only handles single bets now. Parlay logic needs its own route.
    game_id = to_int(request.form.get('game_id'))
    prop_id = to_int(request.form.get('prop_id'))
    bet_type = request.form.get('bet_type')
    selection = request.form.get('selection')
    stake = to_decimal(request.form.get('stake'))

    if not all([bet_type, selection, stake]) or stake <= 0:
        flash('Invalid bet information provided.', 'danger')
        return redirect(request.referrer or url_for('index'))

    if current_user.balance < stake:
        flash('Insufficient balance.', 'danger')
        return redirect(url_for('account'))

    odds, line, target_game_id = None, None, None
    
    if prop_id:
        prop = Prop.query.get_or_404(prop_id)
        if prop.status != 'open':
            flash('This prop is closed for betting.', 'warning')
            return redirect(url_for('game_detail', game_id=prop.game_id))
        target_game_id = prop.game_id
        if prop.prop_type == 'OU':
            line, odds = prop.line, prop.over_odds if selection == 'OVER' else prop.under_odds
        else: # YN
            line, odds = None, prop.yes_odds if selection == 'YES' else prop.no_odds
    elif game_id:
        game = Game.query.get_or_404(game_id)
        if game.status != 'open':
            flash('Betting is closed for this game.', 'warning')
            return redirect(url_for('game_detail', game_id=game.id))
        target_game_id = game.id
        if bet_type == 'ML':
            odds = game.ml_home if selection == 'HOME' else game.ml_away
        elif bet_type == 'SPREAD':
            line, odds = game.spread_line, game.spread_home_odds if selection == 'HOME' else game.spread_away_odds
        elif bet_type == 'TOTAL':
            line, odds = game.total_points, game.over_odds if selection == 'OVER' else game.under_odds
    else:
        flash('A valid game or prop must be selected.', 'danger')
        return redirect(url_for('index'))

    if odds is None:
        flash('This market is not available for betting.', 'danger')
        return redirect(request.referrer or url_for('index'))

    current_user.balance -= stake
    bet = Bet(user_id=current_user.id, game_id=target_game_id, prop_id=prop_id,
              bet_type=bet_type, selection=selection, odds=odds, line=line, stake=stake)
    db.session.add(bet)
    db.session.commit()
    flash('Bet placed successfully!', 'success')
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

# ------------------------------ Admin --------------------------------

@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin_users.html', users=users)

@app.route('/admin/games')
@login_required
@admin_required
def admin_games():
    games = Game.query.order_by(Game.start_time.desc()).all()
    return render_template('admin_games.html', games=games)

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

@app.route("/admin/games/<int:game_id>/grade", methods=["POST"])
@login_required
@admin_required
def admin_grade_game(game_id):
    game = Game.query.get_or_404(game_id)
    
    home_score = to_int(request.form.get("home_score"))
    away_score = to_int(request.form.get("away_score"))

    if home_score is None or away_score is None:
        flash("Invalid scores provided.", "danger")
        return redirect(url_for("admin_games"))
    
    game.home_score = home_score
    game.away_score = away_score
    
    total_score = home_score + away_score
    margin = home_score - away_score

    # Grade single bets for this game
    pending_bets = Bet.query.filter_by(game_id=game.id, status="pending").all()
    for bet in pending_bets:
        result, payout = "lost", Decimal("0.00")
        if bet.bet_type == "ML":
            winner = "HOME" if margin > 0 else ("AWAY" if margin < 0 else None)
            if winner is None: result, payout = "push", bet.stake
            elif bet.selection == winner: result, payout = "won", bet.stake + american_profit(bet.stake, bet.odds)
        elif bet.bet_type == "SPREAD":
            if margin == bet.line: result, payout = "push", bet.stake
            else:
                home_covers = margin > bet.line
                if (bet.selection == "HOME" and home_covers) or (bet.selection == "AWAY" and not home_covers):
                    result, payout = "won", bet.stake + american_profit(bet.stake, bet.odds)
        elif bet.bet_type == "TOTAL":
            if total_score == bet.line: result, payout = "push", bet.stake
            else:
                is_over = total_score > bet.line
                if (bet.selection == "OVER" and is_over) or (bet.selection == "UNDER" and not is_over):
                    result, payout = "won", bet.stake + american_profit(bet.stake, bet.odds)
        
        bet.status = result
        bet.payout = payout
        if result in ("won", "push"):
            bet.user.balance += payout
            
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
