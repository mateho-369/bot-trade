# ============================================================
#   validate_ml.py
#   ML Model Accuracy and Calibration Validation Script
# ============================================================

import pandas as pd
import numpy as np
import os
import sys
import pickle
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score, roc_auc_score
from sklearn.calibration import calibration_curve
import matplotlib.pyplot as plt

sys.path.append(os.path.dirname(__file__))
import config

def validate():
    print("="*50)
    print("   SMC BOT ML VALIDATION REPORT")
    print("="*50)
    print("NOTE: This is an IN-SAMPLE check (tests on training data),")
    print("so it looks optimistic. For an honest out-of-sample verdict")
    print("run:  python backtest_ml.py")
    print("-"*50)

    # 1. Load Data
    if not os.path.exists(config.TRADE_LOG_PATH):
        print(f"Error: Trade log not found at {config.TRADE_LOG_PATH}")
        return

    df = pd.read_csv(config.TRADE_LOG_PATH)
    df_closed = df.dropna(subset=['outcome'])
    
    if len(df_closed) < 10:
        print(f"Error: Not enough closed trades for validation (found {len(df_closed)}, need 10+)")
        return

    print(f"Analyzing {len(df_closed)} closed trades...")

    # 2. Load Model
    if not os.path.exists(config.MODEL_PATH) or not os.path.exists(config.SCALER_PATH):
        print("Error: Model or Scaler not found.")
        return

    with open(config.MODEL_PATH, 'rb') as f:
        model = pickle.load(f)
    with open(config.SCALER_PATH, 'rb') as f:
        scaler = pickle.load(f)

    # 3. Prepare Features
    FEATURE_COLS = [
        'atr_pct', 'rsi', 'close_vs_ema', 'body_pct', 'hour_utc',
        'bull_sweep', 'bear_sweep', 'bull_bos', 'bear_bos'
    ]
    
    X = df_closed[FEATURE_COLS].values
    y_true = df_closed['outcome'].values
    
    X_scaled = scaler.transform(X)
    
    # 4. Predict
    y_prob = model.predict_proba(X_scaled)[:, 1]
    y_pred = (y_prob >= config.ML_MIN_CONFIDENCE).astype(int)

    # 5. Metrics
    metrics = {
        "Accuracy":  accuracy_score(y_true, y_pred),
        "Precision": precision_score(y_true, y_pred, zero_division=0),
        "Recall":    recall_score(y_true, y_pred, zero_division=0),
        "F1-Score":  f1_score(y_true, y_pred, zero_division=0),
    }
    
    try:
        metrics["ROC-AUC"] = roc_auc_score(y_true, y_prob)
    except:
        metrics["ROC-AUC"] = 0.0

    print("\n--- PERFORMANCE METRICS ---")
    for k, v in metrics.items():
        print(f"{k:10}: {v:.4f}")

    # 6. Calibration Check
    print("\n--- CALIBRATION ANALYSIS ---")
    prob_true, prob_pred = calibration_curve(y_true, y_prob, n_bins=5)
    print("Pred Prob | True Win Rate")
    for p_pred, p_true in zip(prob_pred, prob_true):
        print(f"{p_pred:9.1%} | {p_true:13.1%}")

    # 7. Pattern Analysis
    if os.path.exists(config.PATTERN_DATA_PATH):
        import json
        with open(config.PATTERN_DATA_PATH, 'r') as f:
            patterns = json.load(f)
        print("\n--- PATTERN CLUSTERS ---")
        print(f"Whitelist Clusters: {len(patterns['whitelist'])}")
        print(f"Blacklist Clusters: {len(patterns['blacklist'])}")
        
        for i, p in enumerate(patterns['whitelist']):
            print(f"  W{i}: Win Rate {p['win_rate']:.1%}")
        for i, p in enumerate(patterns['blacklist']):
            print(f"  B{i}: Win Rate {p['win_rate']:.1%}")

    print("\n" + "="*50)

if __name__ == "__main__":
    validate()
