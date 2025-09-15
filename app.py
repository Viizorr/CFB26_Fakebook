import os
from datetime import datetime
from decimal import Decimal

from flask import Flask, render_template, redirect, url_for, flash, request, abort, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
db_path = os.getenv("DATABASE_URL") or f"sqlite:///{os.path.join(BASE_DIR, 'app.db')}"

# Render/Heroku sometimes use the old postgres:// scheme; SQLAlchemy wants postgresql://
if db_path.startswith("postgres://"):
    db_path = db_path.replace("postgres://", "postgresql://", 1)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-change-me')
app.config['SQLALCHEMY_DATABASE_URI'] = db_path
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

login_manager = LoginManager(app)
login_manager.login_view = 'login'
db = SQLAlchemy(app)

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    balance = db.Column(db.Numeric(12, 2), default=Decimal('1000.00'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    def set_password(self, password): self.password_hash = generate_password_hash(password)
    def check_password(self, password): return check_password_hash(self.password_hash, password)

class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    home_team = db.Column(db.String(80), nullable=False)
    away_team = db.Column(db.String(80), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default='open', nullable=False)  # open, closed, graded
    ml_home = db.Column(db.Integer, nullable=True)
    ml_away = db.Column(db.Integer, nullable=True)
    spread_line = db.Column(db.Numeric(5,2), nullable=True)  # relative to home team
    spread_home_odds = db.Column(db.Integer, nullable=True)
    spread_away_odds = db.Column(db.Integer, nullable=True)
    total_points = db.Column(db.Numeric(5,2), nullable=True)
    over_odds = db.Column(db.Integer, nullable=True)
    under_odds = db.Column(db.Integer, nullable=True)
    home_score = db.Column(db.Integer, nullable=True)
    away_score = db.Column(db.Integer, nullable=True)

class Bet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), nullable=False)
    bet_type = db.Column(db.String(10), nullable=False)       # ML, SPREAD, TOTAL
    selection = db.Column(db.String(10), nullable=False)      # HOME/AWAY or OVER/UNDER
    odds = db.Column(db.Integer, nullable=False)              # American odds snapshot
    line = db.Column(db.Numeric(5,2), nullable=True)          # spread/total line snapshot
    stake = db.Column(db.Numeric(12,2), nullable=False)
    status = db.Column(db.String(10), default='pending', nullable=False)  # pending, won, lost, push
    payout = db.Column(db.Numeric(12,2), default=Decimal('0.00'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship('User')
    game = db.relationship('Game')
    __table_args__ = (CheckConstraint(stake > 0, name='ck_bet_positive_stake'),)

class ParlayBet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    stake = db.Column(db.Numeric(12,2), nullable=False)
    status = db.Column(db.String(10), default='pending', nullable=False)  # pending, won, lost, push
    payout = db.Column(db.Numeric(12,2), default=Decimal('0.00'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship('User')
    legs = db.relationship('ParlayLeg', backref='parlay', cascade='all, delete-orphan')

class ParlayLeg(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    parlay_id = db.Column(db.Integer, db.ForeignKey('parlay_bet.id'), nullable=False)
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), nullable=False)
    bet_type = db.Column(db.String(10), nullable=False)    # ML, SPREAD, TOTAL
    selection = db.Column(db.String(10), nullable=False)   # HOME/AWAY or OVER/UNDER
    odds = db.Column(db.Integer, nullable=False)
    line = db.Column(db.Numeric(5,2), nullable=True)
    result = db.Column(db.String(10), default='pending', nullable=False)  # pending, won, lost, push


@login_manager.user_loader
def load_user(user_id): return User.query.get(int(user_id))

def admin_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user.is_authenticated or not current_user.is_admin: abort(403)
        return fn(*a, **kw)
    return wrapper

def american_profit(stake: Decimal, odds: int) -> Decimal:
    stake = Decimal(stake)
    if odds > 0:
        return (stake * Decimal(odds) / Decimal(100)).quantize(Decimal('0.01'))
    else:
        return (stake * Decimal(100) / Decimal(abs(odds))).quantize(Decimal('0.01'))

@app.route('/healthz')
def healthz():
    return "ok", 200

@app.route('/')
@login_required
def index():
    # Show open games first, then everything that’s closed or graded
    open_games = Game.query.filter_by(status='open').order_by(Game.start_time.asc()).all()
    past_games = Game.query.filter(Game.status != 'open').order_by(Game.start_time.desc()).all()
    return render_template('index.html', open_games=open_games, past_games=past_games)


@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        u = request.form.get('username','').strip()
        p = request.form.get('password','')
        user = User.query.filter_by(username=u).first()
        if user and user.check_password(p):
            login_user(user)
            return redirect(url_for('index'))
        flash('Invalid credentials', 'danger')
    return render_template('login.html')

@app.route('/register', methods=['GET','POST'])
def register():
    if not ALLOW_SELF_SIGNUP: abort(404)
    if request.method == 'POST':
        u = request.form.get('username','').strip()
        p = request.form.get('password','')
        if not u or not p:
            flash('Username and password required', 'danger'); return render_template('register.html')
        if User.query.filter_by(username=u).first():
            flash('Username already taken', 'warning'); return render_template('register.html')
        user = User(username=u); user.set_password(p)
        db.session.add(user); db.session.commit()
        flash('Registered! Please login.', 'success'); return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user(); return redirect(url_for('login'))

@app.route('/game/<int:game_id>')
@login_required
def game_detail(game_id):
    game = Game.query.get_or_404(game_id)
    return render_template('game_detail.html', game=game)

@app.route('/bet', methods=['POST'])
@login_required
def place_bet():
    game_id = int(request.form['game_id'])
    bet_type = request.form['bet_type']        # ML / SPREAD / TOTAL
    selection = request.form['selection']      # HOME/AWAY or OVER/UNDER
    stake = Decimal(request.form['stake'])
    game = Game.query.get_or_404(game_id)
    if game.status != 'open':
        flash('Betting closed for this game', 'warning'); return redirect(url_for('index'))

    odds, line = None, None
    if bet_type == 'ML':
        odds = game.ml_home if selection == 'HOME' else game.ml_away
    elif bet_type == 'SPREAD':
        line = game.spread_line
        odds = game.spread_home_odds if selection == 'HOME' else game.spread_away_odds
    elif bet_type == 'TOTAL':
        line = game.total_points
        odds = game.over_odds if selection == 'OVER' else game.under_odds
    if odds is None:
        flash('This market is not available', 'danger')
        return redirect(url_for('game_detail', game_id=game.id))

    if current_user.balance < stake:
        flash('Insufficient balance', 'danger')
        return redirect(url_for('game_detail', game_id=game.id))

    current_user.balance = (Decimal(current_user.balance) - stake).quantize(Decimal('0.01'))
    bet = Bet(user_id=current_user.id, game_id=game.id, bet_type=bet_type,
              selection=selection, odds=odds, line=line, stake=stake)
    db.session.add(bet); db.session.commit()
    flash('Bet placed!', 'success'); return redirect(url_for('account'))

@app.route('/account')
@login_required
def account():
    bets = Bet.query.filter_by(user_id=current_user.id).order_by(Bet.created_at.desc()).all()
    return render_template('account.html', bets=bets)

@app.route('/leaderboard')
@login_required
def leaderboard():
    users = User.query.order_by(User.balance.desc()).all()
    return render_template('leaderboard.html', users=users)

# ---------------- Admin ----------------
@app.route('/admin/games')
@login_required
@admin_required
def admin_games():
    games = Game.query.order_by(Game.start_time.desc()).all()
    return render_template('admin_games.html', games=games)

@app.route('/admin/games/new', methods=['GET','POST'])
@login_required
@admin_required
def admin_new_game():
    if request.method == 'POST':
        def i(name):
            v = request.form.get(name)
            return int(v) if v not in (None, '') else None
        def d(name):
            v = request.form.get(name)
            return Decimal(v) if v not in (None, '') else None

        game = Game(
            home_team=request.form['home_team'].strip(),
            away_team=request.form['away_team'].strip(),
            start_time=datetime.fromisoformat(request.form['start_time']),
            ml_home=i('ml_home'),
            ml_away=i('ml_away'),
            spread_line=d('spread_line'),
            spread_home_odds=i('spread_home_odds'),
            spread_away_odds=i('spread_away_odds'),
            total_points=d('total_points'),
            over_odds=i('over_odds'),
            under_odds=i('under_odds'),
        )
        db.session.add(game)
        db.session.commit()
        return redirect(url_for('admin_games'))
    return render_template('admin_edit_game.html', game=None)

@app.route('/admin/games/<int:game_id>/edit', methods=['GET','POST'])
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

        game.home_team = request.form['home_team'].strip()
        game.away_team = request.form['away_team'].strip()
        game.start_time = datetime.fromisoformat(request.form['start_time'])
        game.status = request.form.get('status', 'open')
        game.ml_home = i('ml_home')
        game.ml_away = i('ml_away')
        game.spread_line = d('spread_line')
        game.spread_home_odds = i('spread_home_odds')
        game.spread_away_odds = i('spread_away_odds')
        game.total_points = d('total_points')
        game.over_odds = i('over_odds')
        game.under_odds = i('under_odds')
        db.session.commit()
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

# NEW: Delete game (only if no bets or parlay legs exist)
@app.route('/admin/games/<int:game_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_game(game_id):
    game = Game.query.get_or_404(game_id)
    force = request.form.get('force') == '1'

    # Check if there are any linked bets/legs
    has_single = db.session.query(Bet.id).filter_by(game_id=game_id).first() is not None
    has_parlay_leg = db.session.query(ParlayLeg.id).filter_by(game_id=game_id).first() is not None

    if (has_single or has_parlay_leg) and not force:
        flash('This game has bets. Use Force Delete to refund/reverse and remove everything.', 'warning')
        return redirect(request.referrer or url_for('index'))

    # Force deletion: reverse balances, delete related bets & parlays
    if force:
        # ---- Single bets on this game ----
        singles = Bet.query.filter_by(game_id=game_id).all()
        for b in singles:
            u = b.user
            if b.status in ('pending', 'lost'):
                # restore the original stake
                u.balance = (Decimal(u.balance) + Decimal(b.stake)).quantize(Decimal('0.01'))
            elif b.status == 'won':
                # remove only the profit that was added on grade
                profit = Decimal(b.payout) - Decimal(b.stake)
                u.balance = (Decimal(u.balance) - profit).quantize(Decimal('0.01'))
            elif b.status == 'push':
                # stake was already returned — nothing to do
                pass
            db.session.delete(b)

        # ---- Parlays that include this game: delete whole parlay ----
        leg_rows = ParlayLeg.query.filter_by(game_id=game_id).all()
        parlay_ids = {l.parlay_id for l in leg_rows}
        for pid in parlay_ids:
            pb = ParlayBet.query.get(pid)
            if not pb:
                continue
            u = pb.user
            if pb.status in ('pending', 'lost'):
                # return stake (pending had stake deducted but no payout; lost had stake lost)
                u.balance = (Decimal(u.balance) + Decimal(pb.stake)).quantize(Decimal('0.01'))
            elif pb.status == 'won':
                # remove the profit credited
                profit = Decimal(pb.payout) - Decimal(pb.stake)
                u.balance = (Decimal(u.balance) - profit).quantize(Decimal('0.01'))
            elif pb.status == 'push':
                # stake already refunded
                pass
            db.session.delete(pb)

    # finally remove the game itself
    db.session.delete(game)
    db.session.commit()
    flash('Game and related bets removed.', 'success')
    return redirect(request.referrer or url_for('index'))

@app.route('/admin/games/<int:game_id>/grade', methods=['POST'])
@login_required
@admin_required
def admin_grade_game(game_id):
    game = Game.query.get_or_404(game_id)
    try:
        game.home_score = int(request.form['home_score'])
        game.away_score = int(request.form['away_score'])
    except Exception:
        flash('Invalid scores', 'danger')
        return redirect(url_for('admin_games'))

    total_points = game.home_score + game.away_score
    margin = game.home_score - game.away_score

    pending = Bet.query.filter_by(game_id=game.id, status='pending').all()
    for bet in pending:
        result = 'lost'
        payout = Decimal('0.00')

        if bet.bet_type == 'ML':
            winner = 'HOME' if margin > 0 else ('AWAY' if margin < 0 else None)
            if winner is None:
                result = 'push'
                payout = bet.stake
            elif bet.selection == winner:
                result = 'won'
                payout = bet.stake + american_profit(bet.stake, bet.odds)

        elif bet.bet_type == 'SPREAD':
            line = Decimal(bet.line)
            home_adjusted = margin - line  # >0 home covers, =0 push, <0 away covers
            if home_adjusted == 0:
                result = 'push'
                payout = bet.stake
            else:
                home_covers = home_adjusted > 0
                pick_home = (bet.selection == 'HOME')
                if (home_covers and pick_home) or (not home_covers and not pick_home):
                    result = 'won'
                    payout = bet.stake + american_profit(bet.stake, bet.odds)

        elif bet.bet_type == 'TOTAL':
            line = Decimal(bet.line)
            if Decimal(total_points) == line:
                result = 'push'
                payout = bet.stake
            else:
                went_over = Decimal(total_points) > line
                pick_over = (bet.selection == 'OVER')
                if went_over == pick_over:
                    result = 'won'
                    payout = bet.stake + american_profit(bet.stake, bet.odds)

        bet.status = result
        bet.payout = payout.quantize(Decimal('0.01'))
        if result in ('won','push'):
            user = bet.user
            user.balance = (Decimal(user.balance) + bet.payout).quantize(Decimal('0.01'))

    game.status = 'graded'
    db.session.commit()
    flash('Game graded and balances updated.', 'success')
    return redirect(url_for('admin_games'))

@app.route('/admin/users', methods=['GET','POST'])
@login_required
@admin_required
def admin_users():
    if request.method == 'POST':
        user_id = int(request.form['user_id'])
        adj = Decimal(request.form['adjust_amount'])
        user = User.query.get_or_404(user_id)
        user.balance = (Decimal(user.balance) + adj).quantize(Decimal('0.01'))
        db.session.commit()
        flash('Balance adjusted', 'success')
        return redirect(url_for('admin_users'))

    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin_users.html', users=users)

with app.app_context():
    db.create_all()
    if not User.query.filter_by(is_admin=True).first():
        admin = User(username='admin', is_admin=True, balance=Decimal('100000.00'))
        admin.set_password('admin123')
        db.session.add(admin); db.session.commit()
        print('Created default admin: admin / admin123')

if __name__ == '__main__':
    app.run(debug=True)
