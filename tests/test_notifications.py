"""
tests/test_notifications.py — Mixtape

Tests for notification creation.

Regression test for Issue #4: rating a song must notify the song's original
sharer, exactly like adding the song to a playlist does. Before the fix,
rate_song() saved the rating but never created a Notification, so this test
would fail (0 notifications instead of 1).
"""

import pytest
from app import create_app, db
from models import User, Song, Notification
from services.notification_service import rate_song, add_to_playlist, get_notifications
from services.playlist_service import create_playlist


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed(app):
    """A sharer who owns a song, and a friend who will rate it."""
    with app.app_context():
        sharer = User(username="aaliya", email="aaliya@example.com")
        rater = User(username="kenji", email="kenji@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Neon City", artist="Static Era", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_a_song_notifies_the_sharer(app, seed):
    """
    Rating someone else's song creates a 'song_rated' notification for the
    sharer. This is the regression test for Issue #4.
    """
    with app.app_context():
        sharer_id = seed["sharer"].id
        rater_id = seed["rater"].id
        song_id = seed["song"].id

        rate_song(rater_id, song_id, 5)

        notifs = get_notifications(sharer_id)
        assert len(notifs) == 1
        assert notifs[0]["type"] == "song_rated"
        assert "kenji" in notifs[0]["body"]
        assert "Neon City" in notifs[0]["body"]


def test_rating_own_song_does_not_notify(app, seed):
    """A user rating their own shared song should not notify themselves."""
    with app.app_context():
        sharer_id = seed["sharer"].id
        song_id = seed["song"].id

        rate_song(sharer_id, song_id, 4)

        notifs = get_notifications(sharer_id)
        assert notifs == []
