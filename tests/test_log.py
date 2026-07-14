"""Tests for log module — setup_logging configuration."""

import logging
import os


class TestSetupLogging:
    def test_default_warning(self, clean_env, mocker):
        mock_basic = mocker.patch("log.logging.basicConfig")
        from log import setup_logging

        setup_logging()
        mock_basic.assert_called_once()
        assert mock_basic.call_args[1]["level"] == logging.WARNING

    def test_debug_level(self, clean_env, mocker):
        os.environ["LOG_LEVEL"] = "DEBUG"
        mock_basic = mocker.patch("log.logging.basicConfig")
        from log import setup_logging

        setup_logging()
        mock_basic.assert_called_once()
        assert mock_basic.call_args[1]["level"] == logging.DEBUG

    def test_case_insensitive(self, clean_env, mocker):
        os.environ["LOG_LEVEL"] = "debug"
        mock_basic = mocker.patch("log.logging.basicConfig")
        from log import setup_logging

        setup_logging()
        mock_basic.assert_called_once()
        assert mock_basic.call_args[1]["level"] == logging.DEBUG

    def test_invalid_falls_back(self, clean_env, mocker):
        os.environ["LOG_LEVEL"] = "BOGUS"
        mock_basic = mocker.patch("log.logging.basicConfig")
        from log import setup_logging

        setup_logging()
        mock_basic.assert_called_once()
        assert mock_basic.call_args[1]["level"] == logging.WARNING
