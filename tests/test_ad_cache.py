"""Background AD sync: cache for the desk and maintenance of existing AD accounts."""
from datetime import datetime, timedelta
from unittest import mock

from test_assetlotse import AD_CFG, Base

from assetlotse import ad_cache
from assetlotse.ldap_auth import Directory, LDAPError, LDAPUser
from assetlotse.models import DirectoryPerson, Group, User, db, get_setting, set_setting

DOM = "DC=institut,DC=local"
GRP = f"OU=Gruppen,{DOM}"


def _group(cn, member_of=(), sid=None):
    raw = {"objectSid": [sid]} if sid else {}
    return {"type": "searchResEntry", "dn": f"CN={cn},{GRP}", "raw_attributes": raw,
            "attributes": {"cn": cn, "memberOf": [f"CN={g},{GRP}" for g in member_of]}}


def _person(sam, name, groups=(), ou="OU=Personal,OU=Institut," + DOM, guid=None, uac=512, pgid=None, sid=None):
    raw = {}
    if guid:
        raw["objectGUID"] = [bytes.fromhex(guid)]
    if sid:
        raw["objectSid"] = [sid]
    attrs = {"sAMAccountName": sam, "displayName": name, "mail": f"{sam}@institut.de",
             "memberOf": [f"CN={g},{GRP}" for g in groups], "userAccountControl": uac,
             "objectClass": ["top", "person", "organizationalPerson", "user"], "accountExpires": "0"}
    if pgid:
        attrs["primaryGroupID"] = str(pgid)
    return {"type": "searchResEntry", "dn": f"CN={name},{ou}", "attributes": attrs, "raw_attributes": raw}


def fake_directory(people, groups=(), cfg=None):
    d = Directory({**AD_CFG, **(cfg or {})})
    d.service_connection = mock.Mock(return_value=mock.Mock())

    def paged(conn, base, flt, attrs):
        return list(groups) if "objectClass=group" in flt else list(people)
    d._paged_entries = paged
    return d


G1 = "11" * 16
G2 = "22" * 16


class AllUsersTests(Base):
    def test_rules_groups_nested_and_primary(self):
        dom_sid = bytes(range(1, 21))                       # 1 byte revision … – only used as a prefix here
        people = [
            _person("anna", "Anna", ["AL-Mitarbeiter"], guid=G1),
            _person("bert", "Bert", []),                                             # no rule
            _person("dora", "Dora", [], ou="OU=IT,OU=Institut," + DOM),              # OU rule → manager
            _person("emil", "Emil", ["Team-A"]),                                     # nested in AL-Admins
            _person("fritz", "Fritz", [], pgid=1234, sid=dom_sid + (999).to_bytes(4, "little")),  # primary group
            _person("carl", "Carl", ["AL-Mitarbeiter"], uac=514),                    # disabled
        ]
        groups = [_group("AL-Mitarbeiter", sid=dom_sid + (1234).to_bytes(4, "little")),
                  _group("AL-Admins"), _group("Team-A", member_of=["AL-Admins"])]
        found, complete = fake_directory(people, groups, {"ldap_nested_groups": "1"}).all_users()
        self.assertTrue(complete)
        self.assertEqual({u.username: u.role for u in found},
                         {"anna": "reader", "dora": "manager", "emil": "admin", "fritz": "reader"})
        self.assertEqual(next(u for u in found if u.username == "anna").guid, G1)
        # without “nested”, Team-A → AL-Admins does not count
        found, _c = fake_directory(people, groups, {"ldap_nested_groups": "0"}).all_users()
        self.assertNotIn("emil", {u.username for u in found})

    def test_limit_marks_incomplete(self):
        people = [_person(f"p{i}", f"P {i}", ["AL-Mitarbeiter"]) for i in range(5)]
        found, complete = fake_directory(people).all_users(max_users=3)
        self.assertEqual((len(found), complete), (3, False))

    def test_needs_service_account(self):
        with self.assertRaises(LDAPError):
            Directory({**AD_CFG, "ldap_bind_dn": ""}).all_users()


class SyncTests(Base):
    def setUp(self):
        super().setUp()
        for k, v in {**AD_CFG, "ldap_enabled": "1"}.items():
            set_setting(k, v)
        db.session.commit()

    def run_sync(self, people, **kw):
        with mock.patch("assetlotse.ldap_auth.Directory.all_users", return_value=(people, kw.get("complete", True))):
            return ad_cache.sync(force=True)

    @staticmethod
    def person(username, name=None, role="reader", guid="", groups=()):
        return LDAPUser(username=username, dn=f"CN={name or username},OU=Personal,{DOM}",
                        display_name=name or username.title(), email=f"{username}@institut.de", role=role,
                        guid=guid, groups=[f"CN={g},{GRP}" for g in groups])

    def ad_account(self, username, **kw):
        u = User(username=username, display_name=kw.pop("name", username.title()), auth_source="ldap",
                 role=kw.pop("role", "reader"), ldap_dn=kw.pop("ldap_dn", f"CN={username},OU=Personal,{DOM}"), **kw)
        db.session.add(u)
        db.session.commit()
        return u

    # ---- cache

    def test_cache_holds_only_people_without_account_and_drops_the_rest(self):
        self.run_sync([self.person("anna"), self.person("bert"), self.person("mia")])   # mia has a local account
        self.assertEqual({p.username for p in DirectoryPerson.query}, {"anna", "bert"})
        self.assertTrue(ad_cache.ready())
        self.run_sync([self.person("anna", "Anna Neu")])          # bert disabled/deleted, anna renamed
        rows = {p.username: p.display_name for p in DirectoryPerson.query}
        self.assertEqual(rows, {"anna": "Anna Neu"})

    def test_failed_or_incomplete_sync_keeps_old_state(self):
        self.run_sync([self.person("anna"), self.person("bert")])
        with mock.patch("assetlotse.ldap_auth.Directory.all_users", side_effect=LDAPError("DC weg")):
            self.assertIn("error", ad_cache.sync(force=True))
        self.assertEqual(DirectoryPerson.query.count(), 2)
        self.assertEqual(get_setting("ldap_cache_error"), "DC weg")
        self.run_sync([self.person("anna")], complete=False)
        self.assertEqual(DirectoryPerson.query.count(), 2)       # incomplete: nothing deleted
        self.assertIn("unvollständig", get_setting("ldap_cache_warning"))

    def test_due_interval_retry_and_claim(self):
        now = datetime(2026, 10, 23, 12, 0)
        self.assertTrue(ad_cache.due(now))
        with mock.patch("assetlotse.ldap_auth.Directory.all_users", return_value=([], True)):
            self.assertIsNotNone(ad_cache.sync(now=now))
            self.assertIsNone(ad_cache.sync(now=now + timedelta(minutes=30)))     # hourly
            self.assertIsNotNone(ad_cache.sync(now=now + timedelta(minutes=61)))
        set_setting("ldap_cache_running", (datetime.now()).isoformat(timespec="seconds"))   # another process
        db.session.commit()
        self.assertIsNone(ad_cache.sync(force=True))

    def test_switching_off_clears(self):
        self.run_sync([self.person("anna")])
        set_setting("ldap_cache_enabled", "0")
        db.session.commit()
        gone = self.ad_account("weg", active=False, deactivated_by_sync=True)
        ad_cache.maintain()
        self.assertEqual(DirectoryPerson.query.count(), 0)
        self.assertFalse(get_setting("ldap_cache_synced_at"))
        db.session.refresh(gone)
        self.assertFalse(gone.active)            # departed people do not come back

    def test_search_escapes_and_skips_accounts(self):
        self.run_sync([self.person("a_b", "A_B"), self.person("axb", "AxB")])
        self.assertEqual([p.username for p in ad_cache.search("a_")], ["a_b"])
        self.ad_account("axb")
        self.assertEqual(ad_cache.search("ax"), [])

    # ---- existing accounts

    def test_name_email_and_groups_are_updated(self):
        it = Group.query.filter_by(name="IT").one()
        it.ldap_dn = "AL-IT"
        set_setting("ldap_group_sync", "1")
        u = self.ad_account("anna", name="Anna Alt", email="alt@x.de", last_login=datetime.now())
        self.run_sync([self.person("anna", "Anna Neu", groups=["AL-IT"])])
        db.session.expire_all()
        u = db.session.get(User, u.id)
        self.assertEqual((u.display_name, u.email), ("Anna Neu", "anna@institut.de"))
        self.assertEqual([g.name for g in u.groups], ["IT"])
        self.run_sync([self.person("anna", "Anna Neu", groups=[])])                  # removed from the AD group
        db.session.expire_all()
        self.assertEqual(db.session.get(User, u.id).groups, [])

    def test_role_only_for_accounts_that_signed_in(self):
        never = self.ad_account("neu")                        # taken over at the desk, never signed in
        used = self.ad_account("alt", last_login=datetime.now())
        self.run_sync([self.person("neu", role="admin"), self.person("alt", role="manager")])
        db.session.expire_all()
        self.assertEqual(db.session.get(User, never.id).role, "reader")
        self.assertEqual(db.session.get(User, used.id).role, "manager")

    def test_rename_in_ad_is_detected_by_guid(self):
        u = self.ad_account("amueller", ldap_guid=G1)
        loan_owner_id = u.id
        self.run_sync([self.person("aschmidt", "Anna Schmidt", guid=G1)])
        db.session.expire_all()
        u = db.session.get(User, loan_owner_id)
        self.assertEqual((u.username, u.display_name, u.active), ("aschmidt", "Anna Schmidt", True))
        self.assertEqual(DirectoryPerson.query.count(), 0)    # not listed a second time as “without account”

    def test_same_name_but_other_person_is_not_merged(self):
        u = self.ad_account("anna", ldap_guid=G1)
        others = [self.ad_account(f"x{i}") for i in range(9)]   # so that the safety brake does not kick in
        self.run_sync([self.person("anna", guid=G1)] + [self.person(o.username) for o in others])   # initial setup
        self.run_sync([self.person("anna", guid=G2)] + [self.person(o.username) for o in others])
        db.session.expire_all()
        self.assertFalse(db.session.get(User, u.id).active)    # old account: the person no longer exists

    def test_deactivate_and_reactivate(self):
        keep = [self.ad_account(f"k{i}") for i in range(9)]
        gone = self.ad_account("gone")
        manual = self.ad_account("manual", active=False)       # locked by hand
        everyone = [self.person(k.username) for k in keep]
        self.run_sync(everyone + [self.person("gone", guid=G1), self.person("manual", guid=G2)])   # all present
        self.run_sync(everyone)                                  # 1 of 10 missing → automatic
        db.session.expire_all()
        gone = db.session.get(User, gone.id)
        self.assertEqual((gone.active, gone.deactivated_by_sync), (False, True))
        self.run_sync(everyone + [self.person("gone", guid=G1), self.person("manual", guid=G2)])
        db.session.expire_all()
        self.assertTrue(db.session.get(User, gone.id).active)
        self.assertFalse(db.session.get(User, manual.id).active)   # stays locked

    def test_mass_deactivation_needs_confirmation(self):
        accounts = [self.ad_account(f"u{i}") for i in range(10)]
        everyone = [self.person(a.username) for a in accounts]
        self.run_sync(everyone)
        self.run_sync([self.person("u0")])                       # e.g. wrong base DN: 9 of 10 missing
        db.session.expire_all()
        self.assertTrue(all(db.session.get(User, a.id).active for a in accounts))
        self.assertEqual(len(ad_cache.status()["pending"]), 9)
        self.assertEqual(get_setting("ldap_cache_error"), "")    # warning, not an error → normal interval
        self.login("admin")
        html = self.c.get("/admin/verzeichnisdienst").get_data(as_text=True)
        self.assertIn("u1, u2", html)
        self.c.post("/admin/verzeichnisdienst/deaktivieren")
        db.session.expire_all()
        self.assertEqual(sum(db.session.get(User, a.id).active for a in accounts), 1)
        self.assertEqual(ad_cache.status()["pending"], [])

    def test_first_sync_never_deactivates_automatically(self):
        old = self.ad_account("ausgeschieden")
        self.run_sync([self.person(f"p{i}") for i in range(3)])
        db.session.expire_all()
        self.assertTrue(db.session.get(User, old.id).active)
        self.assertEqual(ad_cache.status()["pending"], ["ausgeschieden"])

    def test_small_installation_and_last_admin_are_protected(self):
        boss = self.ad_account("boss", role="admin", last_login=datetime.now())
        for model in User.query.filter(User.auth_source == "local", User.role == "admin"):
            model.active = False                                   # only the AD admin is active
        db.session.commit()
        self.run_sync([self.person("boss", role="admin")])
        self.run_sync([])                                          # e.g. rules saved incorrectly
        db.session.expire_all()
        self.assertTrue(db.session.get(User, boss.id).active)
        self.assertEqual(ad_cache.status()["pending"], ["boss"])
        u = User(username="helfer", role="admin", auth_source="local")
        u.set_password("geheim12345")
        db.session.add(u)
        db.session.commit()
        self.assertEqual(ad_cache.confirm_pending(u), 1)           # now there is another active admin
        db.session.expire_all()
        self.assertFalse(db.session.get(User, boss.id).active)

    def test_local_accounts_never_touched(self):
        self.run_sync([])
        db.session.expire_all()
        self.assertTrue(db.session.get(User, self.mia.id).active)


class DeskCacheTests(SyncTests):
    def test_desk_search_uses_cache_without_live_ad(self):
        self.run_sync([self.person("amueller", "Anna Müller")])
        self.login("vera")
        with mock.patch.object(Directory, "search_users") as live:
            html = self.c.get("/theke?modus=ausgabe&benutzer=Müller").get_data(as_text=True)
            live.assert_not_called()
        self.assertIn("Anna Müller", html)
        self.assertIn("Konto übernehmen", html)
        with mock.patch.object(Directory, "search_users", return_value=([], False)) as live:
            self.c.get("/theke?modus=ausgabe&benutzer=Niemand")
            live.assert_called_once()                            # nothing in the cache → look up live

    def test_people_suggestions(self):
        self.run_sync([self.person("amueller", "Anna Müller")])
        self.login("vera")
        items = self.c.get("/theke/personen?q=m").get_json()["items"]
        names = {i["name"]: i for i in items}
        self.assertIn("Mia", names)                              # local account
        self.assertTrue(names["Mia"]["url"].endswith("benutzer=mia"))
        self.assertNotIn("Anna Müller", names)                   # AD people only from 2 characters
        ad = {i["name"]: i for i in self.c.get("/theke/personen?q=anna").get_json()["items"]}
        self.assertEqual(ad["Anna Müller"]["post"]["username"], "amueller")
        self.login("mia")
        self.assertEqual(self.c.get("/theke/personen?q=m").status_code, 403)

    def test_takeover_removes_from_cache(self):
        self.run_sync([self.person("amueller", "Anna Müller")])
        info = self.person("amueller", "Anna Müller", role="reader", guid=G1)
        self.login("vera")
        with mock.patch.object(Directory, "check_account", return_value=info):
            self.c.post("/theke/ad-konto", data={"username": "amueller"})
        self.assertEqual(DirectoryPerson.query.count(), 0)
        self.assertEqual(User.query.filter_by(username="amueller").one().ldap_guid, G1)

    def test_admin_page_shows_status_and_sync_now(self):
        self.run_sync([self.person("anna")])
        self.login("admin")
        html = self.c.get("/admin/verzeichnisdienst").get_data(as_text=True)
        self.assertIn("Zuletzt abgeglichen", html)
        self.assertIn("1 Person ohne AssetLotse-Konto", html)
        with mock.patch("assetlotse.ldap_auth.Directory.all_users", return_value=([], True)):
            self.c.post("/admin/verzeichnisdienst/abgleichen")
        self.assertEqual(DirectoryPerson.query.count(), 0)


class ReviewFixTests(SyncTests):
    """Findings from the code review."""

    def login_ad(self, username, info):
        with mock.patch("assetlotse.views.main.ldap_enabled", return_value=True), \
                mock.patch("assetlotse.views.main.authenticate", return_value=info):
            self.c = self.app.test_client()
            return self.c.post("/login", data={"username": username, "password": "ad-pw"})

    def test_rename_then_login_before_sync_keeps_one_account(self):
        old = self.ad_account("amueller", ldap_guid=G1, last_login=datetime.now())
        others = [self.ad_account(f"x{i}") for i in range(9)]
        self.run_sync([self.person("amueller", guid=G1)] + [self.person(o.username) for o in others])
        info = self.person("aschmidt", "Anna Schmidt", guid=G1)
        self.assertEqual(self.login_ad("aschmidt", info).status_code, 302)
        self.assertEqual(User.query.filter_by(ldap_guid=G1).count(), 1)        # no second account
        db.session.expire_all()
        self.assertEqual(db.session.get(User, old.id).username, "aschmidt")
        self.run_sync([info] + [self.person(o.username) for o in others])
        db.session.expire_all()
        self.assertTrue(db.session.get(User, old.id).active)

    def test_duplicate_accounts_for_one_person_are_left_alone(self):
        a = self.ad_account("amueller", ldap_guid=G1)
        b = self.ad_account("aschmidt", ldap_guid=G1)
        others = [self.ad_account(f"x{i}") for i in range(9)]
        rest = [self.person(o.username) for o in others]
        self.run_sync([self.person("aschmidt", guid=G1)] + rest)
        self.run_sync([self.person("aschmidt", guid=G1)] + rest)
        db.session.expire_all()
        self.assertTrue(db.session.get(User, a.id).active and db.session.get(User, b.id).active)
        self.assertIn("amueller", get_setting("ldap_cache_warning"))

    def test_login_reactivates_account_deactivated_by_sync(self):
        u = self.ad_account("weg", active=False, deactivated_by_sync=True, ldap_guid=G1)
        self.assertEqual(self.login_ad("weg", self.person("weg", guid=G1)).status_code, 302)
        db.session.expire_all()
        self.assertEqual((db.session.get(User, u.id).active, db.session.get(User, u.id).deactivated_by_sync),
                         (True, False))
        manual = self.ad_account("gesperrt", active=False)
        self.assertEqual(self.login_ad("gesperrt", self.person("gesperrt")).status_code, 401)
        self.assertIsNotNone(manual)

    def test_login_with_reused_name_of_other_person_is_refused(self):
        self.ad_account("anna", ldap_guid=G1)
        self.assertEqual(self.login_ad("anna", self.person("anna", guid=G2)).status_code, 401)

    def test_switching_off_keeps_departed_locked(self):
        # Since 1.9.2: switching off does not bring back departed people (desk, reminder mails).
        # People who are authorised again are reactivated by the AD sign-in (the flag stays set for that).
        u = self.ad_account("weg", active=False, deactivated_by_sync=True)
        set_setting("ldap_cache_enabled", "0")
        db.session.commit()
        ad_cache.maintain()
        db.session.expire_all()
        self.assertFalse(db.session.get(User, u.id).active)
        self.assertTrue(db.session.get(User, u.id).deactivated_by_sync)

    def test_renames_never_break_on_swapped_names(self):
        a = self.ad_account("x", ldap_guid=G1)
        b = self.ad_account("y", ldap_guid=G2)
        rest = [self.ad_account(f"z{i}") for i in range(9)]
        r = [self.person(o.username) for o in rest]
        self.run_sync([self.person("x", guid=G1), self.person("y", guid=G2)] + r)
        res = self.run_sync([self.person("y", guid=G1), self.person("x", guid=G2)] + r)   # names swapped
        self.assertNotIn("error", res)
        db.session.expire_all()
        self.assertEqual({db.session.get(User, a.id).username, db.session.get(User, b.id).username}, {"x", "y"})


class PartialReadTests(Base):
    def test_aborted_group_read_raises_and_user_size_limit_marks_incomplete(self):
        d = Directory({**AD_CFG})
        conn = mock.Mock()
        conn.extend.standard.paged_search.return_value = iter([])
        conn.result = {"result": 3, "description": "timeLimitExceeded"}
        d.service_connection = mock.Mock(return_value=conn)
        with self.assertRaises(LDAPError):
            d.all_users()
        calls = {"n": 0}

        def paged(base, flt, scope, attributes=None, paged_size=0, generator=True):
            calls["n"] += 1
            conn.result = {"result": 0} if calls["n"] == 1 else {"result": 4, "description": "sizeLimitExceeded"}
            return iter([_person("anna", "Anna", ["AL-Mitarbeiter"])] if calls["n"] == 2 else [])
        conn.extend.standard.paged_search.side_effect = paged
        found, complete = d.all_users()
        self.assertEqual([u.username for u in found], ["anna"])
        self.assertFalse(complete)

    def test_ambiguous_usernames_are_dropped(self):
        people = [_person("anna", "Anna A", ["AL-Mitarbeiter"]),
                  _person("anna", "Anna B", [], ou="OU=Alt," + DOM), _person("bob", "Bob", ["AL-Mitarbeiter"])]
        found, _c = fake_directory(people).all_users()
        self.assertEqual([u.username for u in found], ["bob"])

    def test_openldap_member_lists_only_without_memberof(self):
        groups = [{"type": "searchResEntry", "dn": f"CN=AL-Admins,{GRP}", "raw_attributes": {},
                   "attributes": {"cn": "AL-Admins", "memberUid": ["bob"]}}]
        bob = _person("bob", "Bob", ["AL-Mitarbeiter"])
        found, _c = fake_directory([bob], groups, {"ldap_user_attr": "uid"}).all_users()
        self.assertEqual({u.username: u.role for u in found}, {})   # uid attribute missing in the fake → nobody
        bob["attributes"]["uid"] = "bob"
        found, _c = fake_directory([bob], groups, {"ldap_user_attr": "uid"}).all_users()
        self.assertEqual({u.username: u.role for u in found}, {"bob": "reader"})   # like login: memberOf is enough


class SecondReviewTests(SyncTests):
    """Findings from the second review."""

    def login_ad(self, username, info):
        with mock.patch("assetlotse.views.main.ldap_enabled", return_value=True), \
                mock.patch("assetlotse.views.main.authenticate", return_value=info):
            self.c = self.app.test_client()
            return self.c.post("/login", data={"username": username, "password": "ad-pw"})

    def test_typo_in_admin_rule_never_removes_all_admins(self):
        for model in User.query.filter(User.auth_source == "local"):
            model.active = False
        db.session.commit()
        admins = [self.ad_account(f"adm{i}", role="admin", last_login=datetime.now(), ldap_guid=f"{i:032x}")
                  for i in range(3)]
        staff = [self.ad_account(f"s{i}", last_login=datetime.now(), ldap_guid=f"{i + 10:032x}") for i in range(7)]
        everyone = [self.person(a.username, role="admin", guid=a.ldap_guid) for a in admins] + \
                   [self.person(s.username, guid=s.ldap_guid) for s in staff]
        self.run_sync(everyone)
        for p in everyone:
            p.role = "reader"                                   # typo in the admin rule: everyone is only staff now
        self.run_sync(everyone)
        db.session.expire_all()
        self.assertTrue(all(db.session.get(User, a.id).role == "admin" for a in admins))
        self.assertIn("nicht herabgestuft", get_setting("ldap_cache_warning"))

    def test_mass_role_drop_is_not_applied(self):
        users = [self.ad_account(f"m{i}", role="manager", last_login=datetime.now(), ldap_guid=f"{i:032x}")
                 for i in range(5)]
        people = [self.person(u.username, role="manager", guid=u.ldap_guid) for u in users]
        self.run_sync(people)
        for p in people[:3]:
            p.role = "reader"
        self.run_sync(people)
        db.session.expire_all()
        self.assertTrue(all(db.session.get(User, u.id).role == "manager" for u in users))
        people[0].role = "manager"
        people[1].role = "manager"
        self.run_sync(people)                                  # only 1 of 5 → applied normally
        db.session.expire_all()
        self.assertEqual(db.session.get(User, users[2].id).role, "reader")

    def test_admins_are_never_deactivated_automatically(self):
        boss = self.ad_account("boss", role="admin", ldap_guid=G1)
        rest = [self.ad_account(f"r{i}", ldap_guid=f"{i + 5:032x}") for i in range(9)]
        r = [self.person(u.username, guid=u.ldap_guid) for u in rest]
        self.run_sync(r + [self.person("boss", role="admin", guid=G1)])
        self.run_sync(r)                                       # 1 of 10 missing – but an admin
        db.session.expire_all()
        self.assertTrue(db.session.get(User, boss.id).active)
        self.assertEqual(ad_cache.status()["pending"], ["boss"])

    def test_first_run_brake_lasts_until_confirmed(self):
        old = self.ad_account("alt")
        rest = [self.ad_account(f"r{i}") for i in range(9)]
        r = [self.person(u.username) for u in rest]
        self.run_sync(r)
        self.run_sync(r)                                       # second run: still only marked
        db.session.expire_all()
        self.assertTrue(db.session.get(User, old.id).active)
        self.assertEqual(ad_cache.status()["pending"], ["alt"])

    def test_confirm_skips_people_who_signed_in_since(self):
        old = self.ad_account("alt", ldap_guid=G1)
        self.run_sync([self.person("x")])
        old.last_login = datetime.now() + timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(ad_cache.confirm_pending(self.admin), 0)

    def test_session_recheck_logs_out_when_name_belongs_to_other_person(self):
        u = self.ad_account("anna", ldap_guid=G1, last_login=datetime.now())
        self.c = self.app.test_client()
        with self.c.session_transaction() as s:
            s["_user_id"] = u.get_id()
            s["_fresh"] = True
            s["ldap_ok_at"] = 0
        with mock.patch.object(Directory, "check_account", return_value=self.person("anna", guid=G2)):
            r = self.c.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        db.session.expire_all()
        self.assertEqual(db.session.get(User, u.id).ldap_guid, G1)

    def test_accounts_without_guid_are_not_claimed_by_dn_or_reactivated(self):
        old = self.ad_account("anna", active=False, deactivated_by_sync=True)   # legacy account without GUID
        newcomer = self.person("anna", "Anna Neu", guid=G2)
        self.run_sync([newcomer])
        db.session.expire_all()
        self.assertFalse(db.session.get(User, old.id).active)
        self.assertIn("anna", get_setting("ldap_cache_warning"))
        self.assertEqual(self.login_ad("anna", newcomer).status_code, 401)   # login does not reactivate it either
        dn_only = self.ad_account("schmidt", ldap_dn=f"CN=Anna Schmidt,OU=Personal,{DOM}")
        self.run_sync([self.person("aschmidt2", "Anna Schmidt", guid="33" * 16)])
        db.session.expire_all()
        self.assertEqual(db.session.get(User, dn_only.id).username, "schmidt")   # not reassigned via the DN

    def test_admin_can_rename_ad_account(self):
        u = self.ad_account("anna", ldap_guid=G1)
        self.login("admin")
        self.c.post(f"/admin/benutzer/{u.id}", data={"username": "anna-alt", "display_name": "Anna", "email": "",
                                                    "role": "reader", "active": "1"})
        db.session.expire_all()
        self.assertEqual(db.session.get(User, u.id).username, "anna-alt")

    def test_ad_admin_cannot_save_rules_that_lock_him_out(self):
        boss = self.ad_account("boss", role="admin", ldap_guid=G1, last_login=datetime.now())
        self.c = self.app.test_client()
        with self.c.session_transaction() as s:
            s["_user_id"] = boss.get_id()
            s["_fresh"] = True
            import time
            s["ldap_ok_at"] = int(time.time())
        form = {**{k: v for k, v in AD_CFG.items() if k != "ldap_bind_password"}, "ldap_enabled": "1",
                "action": "save", "ldap_admin_rules": "Tippfehler"}
        with mock.patch.object(Directory, "check_account", return_value=self.person("boss", role="reader", guid=G1)):
            html = self.c.post("/admin/verzeichnisdienst", data=form).get_data(as_text=True)
        self.assertIn("kein Administrator mehr", html)
        self.assertEqual(get_setting("ldap_admin_rules"), AD_CFG["ldap_admin_rules"])
        form["ldap_enabled"] = ""
        html = self.c.post("/admin/verzeichnisdienst", data=form).get_data(as_text=True)
        self.assertIn("nur der lokale", html)


class FinalReviewTests(SecondReviewTests):
    def test_login_uses_guid_even_if_typed_name_exists(self):
        old = self.ad_account("anna", name="Anna Alt", email="alt@x.de")          # legacy account without GUID, active
        bob = self.ad_account("bob", ldap_guid=G2, last_login=datetime.now())
        self.assertEqual(self.login_ad("anna", self.person("anna", "Bob jetzt Anna", guid=G2)).status_code, 302)
        db.session.expire_all()
        self.assertEqual([u.username for u in User.query.filter_by(ldap_guid=G2)], ["bob"])   # Bob's account
        self.assertEqual(db.session.get(User, old.id).display_name, "Anna Alt")              # untouched
        self.assertIsNotNone(bob)

    def test_locked_legacy_account_is_not_overwritten(self):
        old = self.ad_account("anna", name="Anna Alt", email="alt@x.de", active=False, deactivated_by_sync=True)
        newcomer = self.person("anna", "Anna Neu", guid=G1)
        self.assertEqual(self.login_ad("anna", newcomer).status_code, 401)
        self.login("vera")
        with mock.patch.object(Directory, "check_account", return_value=newcomer):
            html = self.c.post("/theke/ad-konto", data={"username": "anna"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("gesperrtes älteres Konto", html)
        self.run_sync([newcomer])
        db.session.expire_all()
        u = db.session.get(User, old.id)
        self.assertEqual((u.display_name, u.email, u.ldap_guid, u.active), ("Anna Alt", "alt@x.de", "", False))

    def test_trust_cert_has_the_same_lockout_protection(self):
        boss = self.ad_account("boss", role="admin", ldap_guid=G1, last_login=datetime.now())
        self.c = self.app.test_client()
        import time
        with self.c.session_transaction() as s:
            s["_user_id"] = boss.get_id()
            s["_fresh"] = True
            s["ldap_ok_at"] = int(time.time())
        form = {**{k: v for k, v in AD_CFG.items() if k != "ldap_bind_password"}, "ldap_enabled": "",
                "action": "trust_cert", "pending_pem": "x"}
        from assetlotse import ldap_tls
        real = ldap_tls.load_pem_bundle
        with mock.patch("assetlotse.ldap_tls.load_pem_bundle", side_effect=lambda t: ["cert"] if t == "x" else real(t)), \
                mock.patch("assetlotse.ldap_tls.to_pem", return_value="PEM"):
            html = self.c.post("/admin/verzeichnisdienst", data=form).get_data(as_text=True)
        self.assertIn("nur der lokale", html)
        self.assertEqual(get_setting("ldap_enabled"), "1")
