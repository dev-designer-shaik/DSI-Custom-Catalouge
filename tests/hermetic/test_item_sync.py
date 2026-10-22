"""Pins the Item -> Website Item copy rules (website_item_review.py): who may write, what, and for which item."""
import sys
import types

import pytest


class _Flags(dict):
	__getattr__ = dict.get

	def __setattr__(self, k, v):
		self[k] = v


class PermissionError_(Exception):
	pass


@pytest.fixture(autouse=True)
def _restore_modules():
	saved = {k: sys.modules.get(k) for k in ("frappe", "dsi_catalogue.website_item_review")}
	yield
	for k, v in saved.items():
		if v is None:
			sys.modules.pop(k, None)
		else:
			sys.modules[k] = v


def _install_frappe(user="karthika@designershaik.com", website_items=("10000012-40ML",)):
	fr = types.ModuleType("frappe")
	fr.PermissionError = PermissionError_
	fr.flags = _Flags()
	fr.session = types.SimpleNamespace(user=user)
	fr.model = types.SimpleNamespace(no_value_fields=("Section Break", "Column Break", "HTML"), table_fields=("Table", "Table MultiSelect"))
	fr.db = types.SimpleNamespace(exists=lambda dt, f=None: dt == "Website Item" and f["item_code"] in website_items)

	def throw(msg, exc=Exception):
		raise exc(msg)

	fr.throw = throw
	fr.whitelist = lambda *a, **k: (lambda f: f)
	sys.modules["frappe"] = fr
	sys.modules.pop("dsi_catalogue.website_item_review", None)
	import dsi_catalogue.website_item_review as m
	return fr, m


class Row(dict):
	def as_dict(self):
		return dict(self)


class Doc:
	FIELDS = [("item_code", "Data"), ("item_name", "Data"), ("item_group", "Link"), ("stock_uom", "Link"), ("brand", "Link"),
			  ("description", "Text"), ("published", "Check"), ("web_long_description", "Text Editor"), ("route", "Data"),
			  ("tabs", "Table")]

	def __init__(self, doctype="Website Item", is_new=False, **vals):
		self.doctype = doctype
		self._new = is_new
		self.__dict__["vals"] = {k: None for k, _ in self.FIELDS}
		self.vals["tabs"] = []
		self.vals.update(vals)
		self.meta = types.SimpleNamespace(fields=[types.SimpleNamespace(fieldname=k, fieldtype=t) for k, t in self.FIELDS])
		self.before = None
		object.__setattr__(self, "name", vals.get("name") or vals.get("item_code"))

	def get(self, k, default=None):
		return self.vals.get(k, default)

	def __getattr__(self, k):
		if k in ("vals", "before", "meta", "_new", "doctype", "name"):
			raise AttributeError(k)
		return self.vals.get(k)

	def __setattr__(self, k, v):
		if k in self.__dict__.get("vals", {}):
			self.vals[k] = v
		else:
			object.__setattr__(self, k, v)

	def is_new(self):
		return self._new

	def get_doc_before_save(self):
		return self.before


def _pair(**changes):
	base = dict(item_code="10000012-40ML", item_name="OPULENT WOMAN-40ML", description="old", published=1, brand="OPULENT")
	before = Doc(**base)
	after = Doc(**dict(base, **changes))
	after.before = before
	return after


def test_window_names_only_the_item_being_saved():
	fr, m = _install_frappe()
	m.item_sync_begin(Doc("Item", item_code="10000012-40ML", name="10000012-40ML"))
	assert fr.flags.dsi_item_sync == "10000012-40ML"
	assert m.item_sync_active(Doc(item_code="10000012-40ML"))
	assert not m.item_sync_active(Doc(item_code="10000099-OTHER"))
	m.item_sync_end(None)
	assert fr.flags.get("dsi_item_sync") is None
	assert not m.item_sync_active(Doc(item_code="10000012-40ML"))


def test_no_window_for_items_without_a_website_item_or_new_items():
	fr, m = _install_frappe(website_items=())
	d = Doc("Item", name="X")
	m.item_sync_begin(d)
	assert fr.flags.get("dsi_item_sync") is None
	fr, m = _install_frappe()
	n = Doc("Item", is_new=True, name="10000012-40ML")
	m.item_sync_begin(n)
	assert fr.flags.get("dsi_item_sync") is None


def test_write_permission_only_inside_the_window_and_only_write():
	fr, m = _install_frappe()
	web = Doc(item_code="10000012-40ML")
	assert m.has_permission(web, ptype="read") is True
	assert m.has_permission(web, ptype="write") is False            # no window: Carla only, as before
	fr.flags.dsi_item_sync = "10000012-40ML"
	assert m.has_permission(web, ptype="write") is True
	assert m.has_permission(web, ptype="delete") is False
	assert m.has_permission(web, ptype="create") is False
	assert m.has_permission(Doc(item_code="10000099-OTHER"), ptype="write") is False   # another item's Website Item
	assert m.has_permission(Doc("Storefront Review", item_code="10000012-40ML"), ptype="write") is False
	assert m.has_permission(web, ptype="write", user=m.EDITOR) is True


def test_guard_without_window_still_refuses_everyone_but_carla():
	fr, m = _install_frappe()
	with pytest.raises(PermissionError_):
		m.guard(_pair(description="new"), "before_validate")
	fr.session.user = m.EDITOR
	m.guard(_pair(description="new"), "before_validate")


def test_item_copy_may_change_the_synced_fields():
	fr, m = _install_frappe()
	fr.flags.dsi_item_sync = "10000012-40ML"
	m.guard(_pair(description="OPULENT SHAIK NO.33 CLASSIC PARFUM WOMEN (40ML)", item_name="n", brand="b"), "before_validate")


@pytest.mark.parametrize("field,value", [("web_long_description", "x"), ("route", "elsewhere")])
def test_item_copy_may_not_change_website_content(field, value):
	fr, m = _install_frappe()
	fr.flags.dsi_item_sync = "10000012-40ML"
	with pytest.raises(PermissionError_) as e:
		m.guard(_pair(**{field: value}), "before_validate")
	assert field in str(e.value)


def test_item_copy_may_not_edit_a_child_table():
	fr, m = _install_frappe()
	fr.flags.dsi_item_sync = "10000012-40ML"
	d = _pair()
	d.vals["tabs"] = [Row(title="t")]
	with pytest.raises(PermissionError_):
		m.guard(d, "before_validate")


def test_disabling_unpublishes_enabling_never_republishes():
	fr, m = _install_frappe()
	fr.flags.dsi_item_sync = "10000012-40ML"
	down = _pair(published=0)
	m.guard(down, "before_validate")
	assert down.published == 0
	base = _pair()
	base.before.vals["published"] = 0
	base.vals["published"] = 1                     # webshop: item re-enabled -> published = 1
	m.guard(base, "before_validate")
	assert base.published == 0                      # reverted: Carla republishes


def test_a_new_website_item_is_never_part_of_the_copy():
	fr, m = _install_frappe()
	fr.flags.dsi_item_sync = "10000012-40ML"
	d = Doc(item_code="10000012-40ML", is_new=True)
	with pytest.raises(PermissionError_):
		m.guard(d, "before_validate")


def test_delete_and_other_events_stay_editor_only_even_inside_the_window():
	fr, m = _install_frappe()
	fr.flags.dsi_item_sync = "10000012-40ML"
	with pytest.raises(PermissionError_):
		m.guard(_pair(), "on_trash")


def test_stage_is_skipped_for_the_item_copy_only():
	fr, m = _install_frappe()
	fr.flags.dsi_item_sync = "10000012-40ML"
	d = _pair(published=0)
	assert m.stage(d) is None                       # returns before touching storefront_review (not importable here)
	fr.flags.dsi_item_sync = "10000099-OTHER"
	with pytest.raises(Exception):
		m.stage(d)                                  # a different item's window does not skip staging


def test_repointing_a_website_item_to_another_item_leaves_the_window():
	fr, m = _install_frappe()
	fr.flags.dsi_item_sync = "10000012-40ML"
	with pytest.raises(PermissionError_):
		m.guard(_pair(item_code="10000099-OTHER"), "before_validate")   # no longer the item in the window -> Carla only
