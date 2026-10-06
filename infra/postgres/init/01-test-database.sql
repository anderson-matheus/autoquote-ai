-- Separate database for `docker compose exec agent pytest` so tests never touch app data.
CREATE DATABASE autoquote_test OWNER autoquote;
