import sys
import os
import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
import numpy as np
import json

# Mock MT5 before importing other modules
sys.modules['MetaTrader5'] = MagicMock()
import MetaTrader5 as mt5

import config
# Update config for test
config.DATA_ROOT_PATH = "SMC_Bot_Data_Test"
config.MODEL_PATH = f"{config.DATA_ROOT_PATH}/models/smc_model.pkl"
config.SCALER_PATH = f"{config.DATA_ROOT_PATH}/models/smc_scaler.pkl"
config.TRADE_LOG_PATH = f"{config.DATA_ROOT_PATH}/data/trade_log.csv"
config.PATTERN_DATA_PATH = f"{config.DATA_ROOT_PATH}/data/patterns.json"

from ml.model import SMCModel, TradeLogger

class TestBotV17(unittest.TestCase):
    def setUp(self):
        os.makedirs(f"{config.DATA_ROOT_PATH}/models", exist_ok=True)
        os.makedirs(f"{config.DATA_ROOT_PATH}/data", exist_ok=True)

    def test_persistent_paths(self):
        """Verify that TradeLogger uses the persistent DATA_ROOT_PATH"""
        logger = TradeLogger()
        self.assertTrue(os.path.exists(config.TRADE_LOG_PATH))
        self.assertIn("SMC_Bot_Data_Test", logger.path)

    def test_rolling_window_training(self):
        """Verify rolling window logic in training"""
        model = SMCModel()
        # Create dummy data with 600 trades
        data = []
        for i in range(600):
            data.append({
                'outcome': 1 if i % 2 == 0 else 0,
                'atr_pct': 0.1, 'rsi': 50, 'close_vs_ema': 0, 'body_pct': 0.1, 
                'hour_utc': 12, 'bull_sweep': 0, 'bear_sweep': 0, 'bull_bos': 0, 'bear_bos': 0
            })
        df = pd.DataFrame(data)
        
        with patch('sklearn.ensemble.RandomForestClassifier.fit') as mock_fit:
            model.train(df)
            # Should only train on 500 trades (config.MAX_TRAINING_TRADES)
            args, _ = mock_fit.call_args
            self.assertEqual(len(args[0]), 500)

    def test_pattern_loading_saving(self):
        """Verify pattern persistence"""
        model = SMCModel()
        model.patterns = {"whitelist": [{"center": [1,2,3], "win_rate": 0.8}], "blacklist": []}
        model.save_patterns()
        
        self.assertTrue(os.path.exists(config.PATTERN_DATA_PATH))
        
        # Load in new model
        model2 = SMCModel()
        self.assertEqual(len(model2.patterns["whitelist"]), 1)
        self.assertEqual(model2.patterns["whitelist"][0]["win_rate"], 0.8)

if __name__ == '__main__':
    unittest.main()
