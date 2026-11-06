from pymongo.errors import OperationFailure

from Backend.helper.database import safe_create_index


class FakeColl:
    def __init__(self, conflict_on=()):
        self.conflict_on, self.made = conflict_on, []

    async def create_index(self, keys, **kw):
        if keys[0][0] in self.conflict_on:
            raise OperationFailure("IndexKeySpecsConflict", code=86)
        self.made.append(keys[0][0])


async def test_conflicting_index_is_skipped_and_later_ones_still_created():
    c = FakeColl(conflict_on=("telegram.id",))
    for field in ("tmdb_id", "telegram.id", "title"):
        await safe_create_index(c, [(field, 1)])
    assert c.made == ["tmdb_id", "title"]


async def test_other_errors_still_raise():
    class Boom(FakeColl):
        async def create_index(self, keys, **kw):
            raise OperationFailure("not authorized", code=13)
    try:
        await safe_create_index(Boom(), [("x", 1)])
    except OperationFailure:
        return
    raise AssertionError("code 13 must not be swallowed")
