"""Spam classification. Everything outside this package depends only on :class:`SpamClassifier`."""

from tutajev.classifier.port import ClassifierError, SpamClassifier, Verdict

__all__ = ["ClassifierError", "SpamClassifier", "Verdict"]
