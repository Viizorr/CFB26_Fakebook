import os
from datetime import datetime
from decimal import Decimal
import json

from flask import Flask, render_template, redirect, url_for, flash, request, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, InterfaceError

# ------------------------- DB URL & App Setup -------------------------

db_path = os.getenv("DATABASE_URL") or f"sqlite:///{os.path.join(BASE_DIR, 'app.db')}"
if db_path.startswith("postgres://"):
    db_path = db_path.replace("postgres://", "postgresql://", 1)
if db_path.startswith("postgresql://") and "sslmode=" not in db_path:
    db_path += ("&" if "?" in db_path else "?") + "sslmode=require"

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv('SECRET_KEY', '58FEEC8BC8DD1F324832D4064E5F3591'),
    SQLALCHEMY_DATABASE_URI=db_path,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "connect_args": {"sslmode": "require"} if db_path.startswith("postgresql://") else {},
    },
)
db = SQLAlchemy(app)
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


class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    home_team = db.Column(db.String(80), nullable=False)
    away_team = db.Column(db.String(80), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default="open", nullable=False)  # open, closed, graded

    ml_home = db.Column(db.Integer, nullable=True)
    ml_away = db.Column(db.Integer, nullable=True)

    # Spread is relative to HOME (e.g., -3.5 means home -3.5)
    spread_line = db.Column(db.Numeric(5, 2), nullable=True)
    spread_home_odds = db.Column(db.Integer, nullable=True)
    spread_away_odds = db.Column(db.Integer, nullable=True)

    total_points = db.Column(db.Numeric(5, 2), nullable=True)
    over_odds = db.Column(db.Integer, nullable=True)
    under_odds = db.Column(db.Integer, nullable=True)

    home_score = db.Column(db.Integer, nullable=True)
    away_score = db.Column(db.Integer, nullable=True)


class Bet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    game_id = db.Column(db.Integer, db.ForeignKey("game.id"), nullable=False)

    bet_type = db.Column(db.String(10), nullable=False)      # ML, SPREAD, TOTAL
    selection = db.Column(db.String(10), nullable=False)     # HOME/AWAY or OVER/UNDER
    odds = db.Column(db.Integer, nullable=False)             # American odds snapshot
    line = db.Column(db.Numeric(5, 2), nullable=True)        # spread/total line snapshot

    stake = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(10), default="pending", nullable=False)  # pending, won, lost, push
    payout = db.Column(db.Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User")
    game = db.relationship("Game")

    __table_args__ = (
        CheckConstraint("stake > 0", name="ck_bet_positive_stake"),
    )


class ParlayBet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    stake = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(10), default="pending", nullable=False)  # pending, won, lost, push
    payout = db.Column(db.Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User")
    legs = db.relationship("ParlayLeg", backref="parlay", cascade="all, delete-orphan")


class ParlayLeg(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    parlay_id = db.Column(db.Integer, db.ForeignKey("parlay_bet.id"), nullable=False)
    game_id = db.Column(db.Integer, db.ForeignKey("game.id"), nullable=False)

    bet_type = db.Column(db.String(10), nullable=False)   # ML, SPREAD, TOTAL
    selection = db.Column(db.String(10), nullable=False)  # HOME/AWAY or OVER/UNDER
    odds = db.Column(db.Integer, nullable=False)
    line = db.Column(db.Numeric(5, 2), nullable=True)

    result = db.Column(db.String(10), default="pending", nullable=False)  # pending, won, lost, push


# ---------------------------- Helpers --------------------------------

from sqlalchemy.exc import OperationalError, InterfaceError

@login_manager.user_loader
def load_user(user_id):
    uid = int(user_id)
    try:
        return db.session.get(User, uid)
    except (OperationalError, InterfaceError):
        db.session.remove()
        return db.session.get(User, uid)

def admin_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return fn(*a, **kw)
    return wrapper


def american_profit(stake: Decimal, odds: int) -> Decimal:
    stake = Decimal(stake)
    if odds > 0:
        return (stake * Decimal(odds) / Decimal(100)).quantize(Decimal("0.01"))
    else:
        return (stake * Decimal(100) / Decimal(abs(odds))).quantize(Decimal("0.01"))


@app.route("/healthz")
def healthz():
    return "ok", 200


# ----------------------------- Routes --------------------------------

@app.route("/")
@login_required
def index():
    open_games = Game.query.filter_by(status="open").order_by(Game.start_time.asc()).all()
    past_games = Game.query.filter(Game.status != "open").order_by(Game.start_time.desc()).all()
    return render_template("index.html", open_games=open_games, past_games=past_games)


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


# --------------------------- Betting ---------------------------------

@app.route("/bet", methods=["POST"])
@login_required
def place_bet():
    # Parlay?
    bets_json = request.form.get("bets")
    if bets_json:
        try:
            legs = json.loads(bets_json)
        except Exception:
            flash("Invalid parlay data", "danger")
            return redirect(url_for("index"))

        stake = Decimal(request.form["stake"])
        if Decimal(current_user.balance) < stake:
            flash("Insufficient balance", "danger")
            return redirect(url_for("index"))

        parlay_legs = []
        for leg in legs:
            game = Game.query.get_or_404(int(leg["gameId"]))
            if game.status != "open":
                flash(f"Betting closed for {game.home_team} vs {game.away_team}", "warning")
                return redirect(url_for("index"))

            bet_type = leg["betType"]
            selection = leg["selection"]
            odds, line = None, None
            if bet_type == "ML":
                odds = game.ml_home if selection == "HOME" else game.ml_away
            elif bet_type == "SPREAD":
                line = game.spread_line
                odds = game.spread_home_odds if selection == "HOME" else game.spread_away_odds
            elif bet_type == "TOTAL":
                line = game.total_points
                odds = game.over_odds if selection == "OVER" else game.under_odds

            if odds is None:
                flash("One of the parlay legs is invalid.", "danger")
                return redirect(url_for("index"))

            parlay_legs.append({
                "game_id": game.id,
                "bet_type": bet_type,
                "selection": selection,
                "odds": odds,
                "line": line
            })

        current_user.balance = (Decimal(current_user.balance) - stake).quantize(Decimal("0.01"))
        pb = ParlayBet(user_id=current_user.id, stake=stake)
        db.session.add(pb)
        db.session.flush()  # get pb.id

        for leg in parlay_legs:
            db.session.add(ParlayLeg(
                parlay_id=pb.id,
                game_id=leg["game_id"],
                bet_type=leg["bet_type"],
                selection=leg["selection"],
                odds=leg["odds"],
                line=leg["line"]
            ))

        db.session.commit()
        flash("Parlay placed!", "success")
        return redirect(url_for("account"))

    # Single bet
    game_id = int(request.form["game_id"])
    bet_type = request.form["bet_type"]
    selection = request.form["selection"]
    stake = Decimal(request.form["stake"])

    game = Game.query.get_or_404(game_id)
    if game.status != "open":
        flash("Betting closed for this game", "warning")
        return redirect(url_for("index"))

    odds, line = None, None
    if bet_type == "ML":
        odds = game.ml_home if selection == "HOME" else game.ml_away
    elif bet_type == "SPREAD":
        line = game.spread_line
        odds = game.spread_home_odds if selection == "HOME" else game.spread_away_odds
    elif bet_type == "TOTAL":
        line = game.total_points
        odds = game.over_odds if selection == "OVER" else game.under_odds

    if odds is None:
        flash("This market is not available", "danger")
        return redirect(url_for("game_detail", game_id=game.id))

    if Decimal(current_user.balance) < stake:
        flash("Insufficient balance", "danger")
        return redirect(url_for("game_detail", game_id=game.id))

    current_user.balance = (Decimal(current_user.balance) - stake).quantize(Decimal("0.01"))
    db.session.add(Bet(
        user_id=current_user.id, game_id=game.id, bet_type=bet_type,
        selection=selection, odds=odds, line=line, stake=stake
    ))
    db.session.commit()
    flash("Bet placed!", "success")
    return redirect(url_for("account"))


# -------------------- Account / Leaderboard --------------------------

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

@app.route('/admin/games')
@login_required
@admin_required
def admin_games():
    # Show newest first; adjust if you prefer ascending
    games = Game.query.order_by(Game.start_time.desc()).all()
    return render_template('admin_games.html', games=games)

@app.route('/admin/games/<int:game_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_edit_game(game_id):
    game = Game.query.get_or_404(game_id)
    if request.method == 'POST':
        def i(name):
            v = request.form.get(name)
            return int(v) if v not in (None, '') else None
        def d(name):
            v = request.form.get(name)
            return Decimal(v) if v not in (None, '') else None

        game.home_team = request.form.get('home_team', game.home_team).strip()
        game.away_team = request.form.get('away_team', game.away_team).strip()
        if request.form.get('start_time'):
            game.start_time = datetime.fromisoformat(request.form['start_time'])
        game.status = request.form.get('status', game.status)

        game.ml_home = i('ml_home')
        game.ml_away = i('ml_away')
        game.spread_line = d('spread_line')
        game.spread_home_odds = i('spread_home_odds')
        game.spread_away_odds = i('spread_away_odds')
        game.total_points = d('total_points')
        game.over_odds = i('over_odds')
        game.under_odds = i('under_odds')

        db.session.commit()
        flash('Game updated.', 'success')
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


@app.route("/admin/games/new", methods=["GET", "POST"])
@login_required
@admin_required
def admin_new_game():
    if request.method == "POST":
        def i(name):
            v = request.form.get(name)
            return int(v) if v not in (None, "",) else None

        def d(name):
            v = request.form.get(name)
            return Decimal(v) if v not in (None, "",) else None

        g = Game(
            home_team=request.form["home_team"].strip(),
            away_team=request.form["away_team"].strip(),
            start_time=datetime.fromisoformat(request.form["start_time"]),
            ml_home=i("ml_home"),
            ml_away=i("ml_away"),
            spread_line=d("spread_line"),
            spread_home_odds=i("spread_home_odds"),
            spread_away_odds=i("spread_away_odds"),
            total_points=d("total_points"),
            over_odds=i("over_odds"),
            under_odds=i("under_odds"),
        )
        db.session.add(g)
        db.session.commit()
        flash("Game created", "success")
        return redirect(url_for("admin_games"))

    return render_template("admin_edit_game.html", game=None)

@app.route("/admin/games/<int:game_id>/grade", methods=["POST"])
@login_required
@admin_required
def admin_grade_game(game_id):
    game = Game.query.get_or_404(game_id)

    try:
        game.home_score = int(request.form["home_score"])
        game.away_score = int(request.form["away_score"])
    except Exception:
        flash("Invalid scores", "danger")
        return redirect(url_for("admin_games"))

    total_points = game.home_score + game.away_score
    margin = game.home_score - game.away_score

    # ---- Grade single bets ----
    pending = Bet.query.filter_by(game_id=game.id, status="pending").all()
    for bet in pending:
        result = "lost"
        payout = Decimal("0.00")

        if bet.bet_type == "ML":
            winner = "HOME" if margin > 0 else ("AWAY" if margin < 0 else None)
            if winner is None:
                result = "push"; payout = bet.stake
            elif bet.selection == winner:
                result = "won"; payout = bet.stake + american_profit(bet.stake, bet.odds)

        elif bet.bet_type == "SPREAD":
            line = Decimal(bet.line)
            home_adjusted = margin - line  # >0 home covers; 0 push; <0 away covers
            if home_adjusted == 0:
                result = "push"; payout = bet.stake
            else:
                home_covers = home_adjusted > 0
                pick_home = (bet.selection == "HOME")
                if (home_covers and pick_home) or (not home_covers and not pick_home):
                    result = "won"; payout = bet.stake + american_profit(bet.stake, bet.odds)

        elif bet.bet_type == "TOTAL":
            line = Decimal(bet.line)
            if Decimal(total_points) == line:
                result = "push"; payout = bet.stake
            else:
                went_over = Decimal(total_points) > line
                pick_over = (bet.selection == "OVER")
                if went_over == pick_over:
                    result = "won"; payout = bet.stake + american_profit(bet.stake, bet.odds)

        bet.status = result
        bet.payout = payout.quantize(Decimal("0.01"))
        if result in ("won", "push"):
            bet.user.balance = (Decimal(bet.user.balance) + bet.payout).quantize(Decimal("0.01"))

    # ---- Grade parlay legs ----
    legs = ParlayLeg.query.filter_by(game_id=game.id, result="pending").all()
    for leg in legs:
        outcome = "lost"
        if leg.bet_type == "ML":
            winner = "HOME" if margin > 0 else ("AWAY" if margin < 0 else None)
            if winner is None: outcome = "push"
            elif leg.selection == winner: outcome = "won"
        elif leg.bet_type == "SPREAD":
            line = Decimal(leg.line)
            home_adjusted = margin - line
            if home_adjusted == 0: outcome = "push"
            else:
                home_covers = home_adjusted > 0
                pick_home = (leg.selection == "HOME")
                if (home_covers and pick_home) or (not home_covers and not pick_home):
                    outcome = "won"
        elif leg.bet_type == "TOTAL":
            line = Decimal(leg.line)
            if Decimal(total_points) == line: outcome = "push"
            else:
                went_over = Decimal(total_points) > line
                pick_over = (leg.selection == "OVER")
                if went_over == pick_over: outcome = "won"

        leg.result = outcome

    db.session.commit()

    # ---- Resolve completed parlays ----
    parlays = ParlayBet.query.filter_by(status="pending").all()
    for pb in parlays:
        results = [leg.result for leg in pb.legs]
        if any(r == "pending" for r in results):
            continue
        if "lost" in results:
            pb.status = "lost"
        else:
            # All legs are won or push
            multiplier = 1
            for leg in pb.legs:
                if leg.result == "push":
                    continue
                o = int(leg.odds)
                multiplier *= (1 + (o / 100 if o > 0 else 100 / abs(o)))
            payout = Decimal(pb.stake) * Decimal(multiplier)
            pb.status = "won"
            pb.payout = payout.quantize(Decimal("0.01"))
            pb.user.balance = (Decimal(pb.user.balance) + pb.payout).quantize(Decimal("0.01"))

    game.status = "graded"
    db.session.commit()
    flash("Game graded and balances updated.", "success")
    return redirect(url_for("admin_games"))


# ----------------------------- Init ----------------------------------

with app.app_context():
    db.create_all()
    if not User.query.filter_by(is_admin=True).first():
        admin = User(username="admin", is_admin=True, balance=Decimal("100000.00"))
        admin.set_password("admin123")
        db.session.add(admin)
        db.session.commit()
        print("Created default admin: admin / admin123")


if __name__ == "__main__":
    app.run(debug=True)
