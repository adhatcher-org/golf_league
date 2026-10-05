import pytest
from conftest import _extract_csrf
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_matchup_generation import seed_fixture

from golf_league.models import Golfer, PlayerMatch, TeamMatch, User, WeekHandicap


@pytest.fixture
def matches(admin_client):
    with Session(admin_client.app.state.engine) as session:
        ids = seed_fixture(session)
        sub = Golfer(first_name='Sub', last_name='Synthetic', default_tee_set_id=ids['tee'],
                     handicap_strokes=0, handicap_source='self_reported', handicap_status='ok')
        session.add(sub)
        session.commit()
        ids['sub'] = sub.id
    return admin_client, ids


def url(ids):
    return f"/admin/seasons/{ids['season']}/weeks/{ids['weeks'][0]}/matches/new"


def post_create(client, ids, **changes):
    data = {'home_team_id': ids['teams'][0], 'away_team_id': ids['teams'][1],
            'csrf_token': _extract_csrf(client.get(url(ids)).text)}
    data.update(changes)
    return client.post(url(ids), data=data, follow_redirects=False)


def test_public_create_generate_substitute_and_get_read_only(matches):
    client, ids = matches
    assert client.get(url(ids)).status_code == 200
    with Session(client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(TeamMatch)) == 0
    assert post_create(client, ids).status_code == 303
    with Session(client.app.state.engine) as session:
        pairing = session.scalar(select(TeamMatch)).id
        rows = list(session.scalars(select(PlayerMatch).order_by(PlayerMatch.id)))
        row_ids = [r.id for r in rows]
        assert len(rows) == 4
        assert session.scalar(select(func.count()).select_from(WeekHandicap)) == 8
    page = client.get(url(ids))
    assert 'A1 Example' in page.text and 'B1 Example' in page.text
    sub_url = f"/admin/seasons/{ids['season']}/player-matches/{row_ids[0]}/substitute"
    page = client.get(sub_url)
    assert 'Sub Synthetic' in page.text
    csrf = _extract_csrf(page.text)
    assert client.post(sub_url, data={'side':'a', 'golfer_id':ids['sub'], 'csrf_token':csrf}, follow_redirects=False).status_code == 303
    gen_url = f"/admin/seasons/{ids['season']}/team-matches/{pairing}/generate"
    assert client.post(gen_url, data={'csrf_token':_extract_csrf(client.get(url(ids)).text)}, follow_redirects=False).status_code == 303
    with Session(client.app.state.engine) as session:
        assert [r.id for r in session.scalars(select(PlayerMatch).order_by(PlayerMatch.id))] == row_ids
        assert session.get(PlayerMatch, row_ids[0]).a_golfer_id == ids['sub']
        assert session.scalar(select(func.count()).select_from(WeekHandicap)) == 9


@pytest.mark.parametrize('value', ['', '-1', '1.5', '²', 'abc'])
def test_invalid_numeric_form_renders_html_blank(matches, value):
    client, ids = matches
    response = post_create(client, ids, home_team_id=value)
    assert response.status_code == 422 and 'text/html' in response.headers['content-type']
    assert '<option value="">' in response.text
    with Session(client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(TeamMatch)) == 0


def test_csrf_ownership_and_substitution_errors(matches):
    client, ids = matches
    assert client.post(url(ids), data={}).status_code == 403
    assert post_create(client, ids).status_code == 303
    with Session(client.app.state.engine) as session:
        row_ids = list(session.scalars(select(PlayerMatch.id).order_by(PlayerMatch.id)))
        pairing = session.scalar(select(TeamMatch.id))
    csrf = _extract_csrf(client.get(url(ids)).text)
    sub_url = f"/admin/seasons/{ids['season']}/player-matches/{row_ids[0]}/substitute"
    gen_url = f"/admin/seasons/{ids['season']}/team-matches/{pairing}/generate"
    assert client.post(gen_url, data={}).status_code == 403
    assert client.post(sub_url, data={}).status_code == 403
    for path in [url(ids), gen_url, sub_url]:
        foreign = path.replace(f"/seasons/{ids['season']}/", '/seasons/99999/')
        if path != gen_url:
            assert client.get(foreign).status_code == 404
        assert client.post(foreign, data={'csrf_token':csrf}).status_code == 404
    response = client.post(sub_url, data={'side':'z', 'golfer_id':ids['sub'], 'csrf_token':csrf})
    assert response.status_code == 422 and 'A1 Example' in response.text
    assert client.post(sub_url, data={'side':'a', 'golfer_id':ids['sub'], 'csrf_token':csrf}, follow_redirects=False).status_code == 303
    other = f"/admin/seasons/{ids['season']}/player-matches/{row_ids[1]}/substitute"
    assert client.post(other, data={'side':'b', 'golfer_id':ids['sub'], 'csrf_token':csrf}).status_code == 409
    assert post_create(client, ids).status_code == 422


@pytest.mark.parametrize('kind', ['anonymous', 'unverified', 'nonadmin'])
def test_all_match_routes_authorized(matches, kind):
    client, ids = matches
    assert post_create(client, ids).status_code == 303
    with Session(client.app.state.engine) as session:
        row = session.scalar(select(PlayerMatch.id))
        pairing = session.scalar(select(TeamMatch.id))
        user = session.scalar(select(User))
        if kind == 'unverified':
            user.email_verified_at = None
        if kind == 'nonadmin':
            user.is_admin = False
        session.commit()
    csrf = _extract_csrf(client.get('/login').text)
    if kind == 'anonymous':
        client.cookies.clear()
    paths = [url(ids), f"/admin/seasons/{ids['season']}/player-matches/{row}/substitute",
             f"/admin/seasons/{ids['season']}/team-matches/{pairing}/generate"]
    for path in paths:
        if not path.endswith('/generate'):
            assert client.get(path).status_code == (401 if kind == 'anonymous' else 403)
        assert client.post(path, data={'csrf_token':csrf}).status_code == (401 if kind == 'anonymous' else 403)


def test_public_create_snapshot_failure_rolls_back(matches, monkeypatch):
    from golf_league.services import handicaps
    client, ids = matches
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic snapshot failure')
    monkeypatch.setattr(handicaps, 'ensure_week_handicap', fail)
    with pytest.raises(RuntimeError, match='synthetic snapshot failure'):
        post_create(client, ids)
    with Session(client.app.state.engine) as session:
        for model in [TeamMatch, PlayerMatch, WeekHandicap]:
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_public_regenerate_fault_preserves_history(matches, monkeypatch):
    from golf_league.services import handicaps
    client, ids = matches
    assert post_create(client, ids).status_code == 303
    with Session(client.app.state.engine) as session:
        pairing = session.scalar(select(TeamMatch.id))
        before = [(r.id, r.a_golfer_id, r.b_golfer_id) for r in session.scalars(select(PlayerMatch))]
        snapshot_ids = list(session.scalars(select(WeekHandicap.id)))
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic regenerate fault')
    monkeypatch.setattr(handicaps, 'ensure_week_handicap', fail)
    path = f"/admin/seasons/{ids['season']}/team-matches/{pairing}/generate"
    with pytest.raises(RuntimeError, match='synthetic regenerate fault'):
        client.post(path, data={'csrf_token':_extract_csrf(client.get(url(ids)).text)})
    with Session(client.app.state.engine) as session:
        assert [(r.id, r.a_golfer_id, r.b_golfer_id) for r in session.scalars(select(PlayerMatch))] == before
        assert list(session.scalars(select(WeekHandicap.id))) == snapshot_ids


def business_rows(client):
    """Capture all persisted matchup and snapshot fields, including historical metadata."""
    with Session(client.app.state.engine) as session:
        return {
            model.__tablename__: [
                tuple(getattr(row, column.name) for column in model.__table__.columns)
                for row in session.scalars(select(model).order_by(model.id))
            ]
            for model in (TeamMatch, PlayerMatch, WeekHandicap)
        }


@pytest.mark.parametrize('field', ['home_team_id', 'away_team_id', 'golfer_id'])
@pytest.mark.parametrize('value', [
    pytest.param('0', id='zero'),
    pytest.param(str(2**63), id='int64-overflow'),
    pytest.param('9999999999999999999999', id='larger-decimal'),
    pytest.param('9' * 5000, id='5000-digits'),
    pytest.param('²', id='superscript-two'),
])
def test_invalid_id_range_matrix_html_reset_and_no_business_writes(matches, field, value):
    client, ids = matches
    assert post_create(client, ids).status_code == 303
    before = business_rows(client)
    if field == 'golfer_id':
        row_id = before['player_matches'][0][0]
        path = f"/admin/seasons/{ids['season']}/player-matches/{row_id}/substitute"
        data = {'side': 'b', 'golfer_id': value,
                'csrf_token': _extract_csrf(client.get(path).text)}
        response = client.post(path, data=data, follow_redirects=False)
        assert 'Side A: A1 Example' in response.text
        assert 'Side B: B1 Example' in response.text
        assert '<option value="a">A · A1 Example</option>' in response.text
        assert '<option value="b">B · B1 Example</option>' in response.text
        assert '<option value="">Choose substitute</option>' in response.text
    else:
        response = post_create(client, ids, **{field: value})
        assert '<select name="home_team_id" id="home"><option value="">Choose team</option>' in response.text
        assert '<select name="away_team_id" id="away"><option value="">Choose team</option>' in response.text
    assert response.status_code == 422
    assert 'text/html' in response.headers['content-type']
    assert 'Choose a valid player or team.' in response.text
    assert '<div class="error" role="alert">' in response.text
    assert ' selected' not in response.text
    assert business_rows(client) == before


@pytest.mark.parametrize('field', ['home_team_id', 'away_team_id', 'golfer_id'])
@pytest.mark.parametrize('value', [999999, 2**63 - 1], ids=['absent', 'max-int64'])
def test_representable_absent_form_ids_remain_404_without_writes(matches, field, value):
    client, ids = matches
    assert post_create(client, ids).status_code == 303
    before = business_rows(client)
    if field == 'golfer_id':
        path = f"/admin/seasons/{ids['season']}/player-matches/{before['player_matches'][0][0]}/substitute"
        response = client.post(path, data={'side': 'a', 'golfer_id': value,
            'csrf_token': _extract_csrf(client.get(path).text)}, follow_redirects=False)
    else:
        response = post_create(client, ids, **{field: str(value)})
    assert response.status_code == 404
    assert business_rows(client) == before


@pytest.mark.parametrize('field', ['home_team_id', 'away_team_id'])
def test_representable_foreign_team_form_ids_remain_404_without_writes(matches, field):
    client, ids = matches
    assert post_create(client, ids).status_code == 303
    with Session(client.app.state.engine) as session:
        foreign = seed_fixture(session)
    before = business_rows(client)
    assert post_create(client, ids, **{field: foreign['teams'][0]}).status_code == 404
    assert business_rows(client) == before
