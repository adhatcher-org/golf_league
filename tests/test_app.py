

def test_create_app_uses_the_settings_it_is_given(client, tmp_path):
    """Test that create_app uses the settings it's given and creates the database."""
    # Test that /readyz returns 200
    response = client.get("/readyz")
    assert response.status_code == 200

    # Test that the database file was created at the expected location
    db_path = tmp_path / "app.db"
    assert db_path.exists(), f"Database file should exist at {db_path}"
    assert db_path.is_file(), f"{db_path} should be a file"

    # Verify the database is not empty (has at least the schema)
    assert db_path.stat().st_size > 0, "Database file should not be empty"
