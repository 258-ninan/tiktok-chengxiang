"""Evidence of completed pagination for one open Chrome video page."""

from collections import defaultdict

from sources import item_id


def page_coordinates(data):
    parent = item_id(data["parentId"]) if data["parentId"] is not None else None
    cursor = item_id(data["cursor"])
    more = data["hasMore"]
    if type(more) is not bool:
        raise ValueError("Unverified comment pagination")
    next_cursor = item_id(data["nextCursor"]) if more else None
    if next_cursor == cursor:
        raise ValueError("Comment cursor did not advance")
    return parent, cursor, next_cursor


class Coverage:
    def __init__(self):
        self.pages = defaultdict(dict)
        self.roots = {}
        self.reply_ids = defaultdict(set)
        self.invalid_pages = set()
        self.saw_comment_rows = False

    def observe(self, coordinates, comments, *, valid, final):
        parent, cursor, next_cursor = coordinates
        key = (parent, cursor)
        pages = self.pages[parent]
        if not valid or (final and cursor in pages and pages[cursor] != next_cursor):
            self.invalid_pages.add(key)
            pages.pop(cursor, None)
            return
        for row in comments:
            cid = item_id(row["cid"])
            if parent is None and row.get("reply_id") in (None, "", 0, "0"):
                count = row.get("reply_total")
                if type(count) is int and count >= 0:
                    self.roots[cid] = max(count, self.roots.get(cid) or 0)
                else:
                    self.roots.setdefault(cid, None)
            elif parent is not None:
                self.reply_ids[parent].add(cid)
        if final and key not in self.invalid_pages:
            pages[cursor] = next_cursor

    def reached_end(self, parent):
        pages = self.pages.get(parent, {})
        cursor = "0"
        seen = set()
        while cursor in pages and cursor not in seen:
            seen.add(cursor)
            cursor = pages[cursor]
            if cursor is None:
                return True
        return False

    def complete(self):
        if not self.reached_end(None):
            return False
        if not self.roots:
            return not self.saw_comment_rows and self.pages == {None: {"0": None}}
        return all(
            count == 0 or (
                self.reached_end(parent) and len(self.reply_ids[parent]) >= (count or 0)
            )
            for parent, count in self.roots.items()
        )
