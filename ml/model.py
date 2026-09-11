# ============================================================
#   ml/model.py
#   Machine Learning Model for SMC Signal Filtering
#   v1.7.2 - Rolling Window + Pattern Blacklist/Whitelist
# ============================================================

import os
import pickle
import logging
import numpy as np
import pandas as pd
import json
from datetime import datetime, timedelta
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger("MLModel")

FEATURE_COLS = [
    'atr_pct', 'rsi', 'close_vs_ema', 'body_pct', 'hour_utc',
    'bull_sweep', 'bear_sweep', 'bull_bos', 'bear_bos'
]

class SMCModel:
    def __init__(self):
        self.model = None
        self.scaler = None
        self.trained = False
        self.patterns = {"whitelist": [], "blacklist": []}
        self.FEATURE_COLS = FEATURE_COLS
        self.load()
        self.load_patterns()

    def load(self):
        if os.path.exists(config.MODEL_PATH) and os.path.exists(config.SCALER_PATH):
            try:
                with open(config.MODEL_PATH, 'rb') as f:
                    self.model = pickle.load(f)
                with open(config.SCALER_PATH, 'rb') as f:
                    self.scaler = pickle.load(f)
                self.trained = True
                logger.info("ML Model loaded successfully")
            except Exception as e:
                logger.error(f"Error loading ML model: {e}")

    def load_patterns(self):
        if os.path.exists(config.PATTERN_DATA_PATH):
            try:
                with open(config.PATTERN_DATA_PATH, 'r') as f:
                    self.patterns = json.load(f)
                logger.info("Pattern data loaded")
            except Exception as e:
                logger.error(f"Error loading patterns: {e}")

    def save_patterns(self):
        try:
            os.makedirs(os.path.dirname(config.PATTERN_DATA_PATH), exist_ok=True)
            with open(config.PATTERN_DATA_PATH, 'w') as f:
                json.dump(self.patterns, f)
        except Exception as e:
            logger.error(f"Error saving patterns: {e}")

    def train(self, df):
        """Train model on historical trade data (Rolling Window)"""
        try:
            # Use rolling window
            if len(df) > config.MAX_TRAINING_TRADES:
                df = df.tail(config.MAX_TRAINING_TRADES)

            if len(df) < config.ML_MIN_TRADES:
                return False

            from sklearn.ensemble import RandomForestClassifier
            from sklearn.preprocessing import StandardScaler

            X = df[FEATURE_COLS].values
            y = df['outcome'].values

            self.scaler = StandardScaler()
            X_scaled = self.scaler.fit_transform(X)

            # class_weight balanced → don't just predict the majority outcome
            # (win rate ~43% would otherwise bias the model toward "loss").
            self.model = RandomForestClassifier(
                n_estimators=100, random_state=42, class_weight='balanced'
            )
            self.model.fit(X_scaled, y)

            # Save
            os.makedirs(os.path.dirname(config.MODEL_PATH), exist_ok=True)
            with open(config.MODEL_PATH, 'wb') as f:
                pickle.dump(self.model, f)
            with open(config.SCALER_PATH, 'wb') as f:
                pickle.dump(self.scaler, f)

            self.trained = True
            
            # Run clustering for patterns if enabled
            if config.PATTERN_CLUSTERING_ENABLED:
                self.update_patterns(df)
                
            return True
        except Exception as e:
            logger.error(f"Training failed: {e}")
            return False

    def update_patterns(self, df):
        """
        Cluster trades by feature similarity to find high/low win-rate patterns.
        Clustering is done in SCALED feature space so no single feature (e.g. RSI
        0-100, hour 0-23) dominates the distance. Centers are stored scaled so the
        live distance check in match_pattern() is meaningful.
        """
        try:
            from sklearn.cluster import KMeans

            if self.scaler is None:
                return
            X = self.scaler.transform(df[FEATURE_COLS].values)  # scaled space
            n_clusters = min(len(df) // 10, 10)
            if n_clusters < 2: return

            kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
            labels = kmeans.fit_predict(X)
            df = df.copy()
            df['cluster'] = labels

            new_patterns = {"whitelist": [], "blacklist": []}

            for i in range(n_clusters):
                cluster_df = df[df['cluster'] == i]
                if len(cluster_df) < 5: continue

                win_rate = float(cluster_df['outcome'].mean())
                center = kmeans.cluster_centers_[i].tolist()   # scaled center

                if win_rate >= 0.65:
                    new_patterns["whitelist"].append({"center": center, "win_rate": win_rate})
                elif win_rate <= 0.40:
                    new_patterns["blacklist"].append({"center": center, "win_rate": win_rate})

            self.patterns = new_patterns
            self.save_patterns()
            logger.info(f"Patterns updated: {len(self.patterns['whitelist'])} whitelist, {len(self.patterns['blacklist'])} blacklist")

        except Exception as e:
            logger.error(f"Pattern clustering failed: {e}")

    # Distance threshold in STANDARDIZED space (roughly "within ~1.5 std").
    PATTERN_MATCH_DIST = 1.5

    def match_pattern(self, features):
        """
        Return 'whitelist', 'blacklist', or None for a raw feature vector.
        Scales the features and compares to the (scaled) cluster centers.
        Single source of truth used by both predict() and the alert logic.
        """
        if not config.PATTERN_CLUSTERING_ENABLED or self.scaler is None:
            return None
        try:
            fx = self.scaler.transform(np.array([features], dtype=float))[0]
        except Exception:
            return None

        for p in self.patterns.get("whitelist", []):
            if np.linalg.norm(fx - np.array(p["center"])) < self.PATTERN_MATCH_DIST:
                return "whitelist"
        if getattr(config, 'BLACKLIST_ENABLED', True):
            for p in self.patterns.get("blacklist", []):
                if np.linalg.norm(fx - np.array(p["center"])) < self.PATTERN_MATCH_DIST:
                    return "blacklist"
        return None

    def predict(self, signal_dict, spread_pct):
        """Predict win probability for a signal with pattern override"""
        if not self.trained:
            return True, 1.0

        try:
            features = []
            for col in FEATURE_COLS:
                val = signal_dict.get(col, 0)
                features.append(float(val))

            X = np.array([features])
            X_scaled = self.scaler.transform(X)

            # FIX: resolve the WIN class index from the model instead of
            # assuming [0][1] is "win" — if a retraining window ever contains
            # only one class (e.g. only losses), [0][1] was the wrong column
            # or an IndexError (which silently failed OPEN, letting everything
            # through).
            classes = list(self.model.classes_)
            if 1 not in classes:
                # model has never seen a WIN — no evidence to approve anything
                return False, 0.0
            win_idx = classes.index(1)
            prob = self.model.predict_proba(X_scaled)[0][win_idx]

            # Pattern override (nearest cluster center in scaled space)
            match = self.match_pattern(features)
            if match == "whitelist":
                prob += config.WHITELIST_CONFIDENCE_BOOST
                logger.info(f"Pattern WHITELIST match — boosted prob to {min(prob,1.0):.2f}")
            elif match == "blacklist":
                prob = min(prob, config.BLACKLIST_BLOCK_CONFIDENCE - 0.01)
                logger.info(f"Pattern BLACKLIST match — capped prob to {prob:.2f}")

            prob = max(0.0, min(1.0, prob))
            passed = bool(prob >= config.ML_MIN_CONFIDENCE)
            
            return passed, float(prob)
        except Exception as e:
            logger.error(f"Prediction error: {e}")
            return True, 1.0


class TradeLogger:
    def __init__(self):
        os.makedirs(os.path.dirname(config.TRADE_LOG_PATH), exist_ok=True)
        self.path = config.TRADE_LOG_PATH

        if not os.path.exists(self.path):
            cols = ['time', 'symbol', 'ticket', 'signal', 'outcome',
                    'profit', 'entry', 'tp', 'sl', 'atr',
                    'spread_pct'] + FEATURE_COLS
            pd.DataFrame(columns=cols).to_csv(self.path, index=False)
            logger.info(f"Trade log created: {self.path}")
        else:
            try:
                df = pd.read_csv(self.path)
                if 'ticket' not in df.columns:
                    df.insert(2, 'ticket', np.nan)
                    df.to_csv(self.path, index=False)
            except Exception as e:
                logger.error(f"Migration error: {e}")

    def log_trade(self, symbol, signal_dict, entry, tp, sl, spread_pct, ticket=None):
        row = {
            'time':         signal_dict.get('bar_time', pd.Timestamp.now()),
            'symbol':       symbol,
            'ticket':       ticket,
            'signal':       signal_dict.get('signal'),
            'outcome':      np.nan,
            'profit':       np.nan,
            'entry':        entry,
            'tp':           tp,
            'sl':           sl,
            'atr':          signal_dict.get('atr', 0),
            'spread_pct':   spread_pct,
            'atr_pct':      signal_dict.get('atr_pct', 0),
            'rsi':          signal_dict.get('rsi', 50),
            'close_vs_ema': signal_dict.get('close_vs_ema', 0),
            'body_pct':     signal_dict.get('body_pct', 0),
            'hour_utc':     signal_dict.get('hour_utc', 0),
            'bull_sweep':   1 if signal_dict.get('bull_sweep') else 0,
            'bear_sweep':   1 if signal_dict.get('bear_sweep') else 0,
            'bull_bos':     1 if signal_dict.get('bull_bos')   else 0,
            'bear_bos':     1 if signal_dict.get('bear_bos')   else 0,
        }
        df = pd.DataFrame([row])
        cols = ['time', 'symbol', 'ticket', 'signal', 'outcome',
                'profit', 'entry', 'tp', 'sl', 'atr',
                'spread_pct'] + FEATURE_COLS
        df = df[cols]
        df.to_csv(self.path, mode='a', header=False, index=False)
        logger.info(f"Trade logged: {symbol} {signal_dict.get('signal')} | Ticket: {ticket}")

    def update_outcome(self, symbol, ticket, profit):
        try:
            df = pd.read_csv(self.path)
            mask = (df['symbol'] == symbol) & (df['ticket'] == ticket) & (df['outcome'].isna())
            if mask.sum() == 0: return
            idx = df[mask].index[-1]
            df.loc[idx, 'outcome'] = 1 if profit > 0 else 0
            df.loc[idx, 'profit']  = profit
            df.to_csv(self.path, index=False)
            logger.info(f"Outcome updated: {symbol} Ticket:{ticket} → {'WIN' if profit>0 else 'LOSS'}")
        except Exception as e:
            logger.error(f"Error updating outcome: {e}")

    def load(self):
        try:
            return pd.read_csv(self.path, parse_dates=['time'])
        except Exception:
            return pd.DataFrame()

    def get_stats(self):
        df = self.load()
        if df.empty or 'outcome' not in df.columns:
            return {'total': 0, 'wins': 0, 'losses': 0, 'winrate': 0.0}
        df_closed = df.dropna(subset=['outcome'])
        if len(df_closed) == 0:
            return {'total': 0, 'wins': 0, 'losses': 0, 'winrate': 0.0}
        wins   = int(df_closed['outcome'].sum())
        total  = len(df_closed)
        return {'total': total, 'wins': wins, 'losses': total - wins, 'winrate': wins / total * 100}
