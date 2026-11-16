# covers: FR-1, FR-2, FR-4, FR-5, FR-6, NFR-3
import pytest

from app.codegen import CodeGenerator
from app.service import (DAY, AliasConflict, CodeSpaceExhausted, InvalidURL, LinkGone, LinkNotFound,
                         ShortenerService)


def test_create_and_resolve(service):
    link = service.create("https://example.com/a?b=c")
    assert service.resolve(link.code) == "https://example.com/a?b=c"


@pytest.mark.parametrize("url", ["ftp://x.com", "javascript:alert(1)", "example.com", "http://", "",
                                 "http://localhost/admin", "http://10.0.0.5/", "http://127.0.0.1:8080"])
def test_rejects_unsafe_or_invalid_urls(service, url):
    with pytest.raises(InvalidURL):
        service.create(url)


def test_rejects_overlong_url(service):
    with pytest.raises(InvalidURL):
        service.create("https://e.com/" + "a" * 3000)


def test_custom_alias_and_conflict(service):
    service.create("https://example.com/1", custom_alias="promo")
    with pytest.raises(AliasConflict):
        service.create("https://example.com/2", custom_alias="promo")
    assert service.resolve("promo") == "https://example.com/1"


def test_expired_link_is_gone(service, clock):
    link = service.create("https://example.com", ttl_days=1)
    clock.advance(DAY - 1)
    assert service.resolve(link.code) == "https://example.com"
    clock.advance(1)
    with pytest.raises(LinkGone):
        service.resolve(link.code)


def test_unknown_code(service):
    with pytest.raises(LinkNotFound):
        service.resolve("doesnotexist")


def test_deactivate_invalidates_cache(service):
    link = service.create("https://example.com")
    service.resolve(link.code)          # warm cache
    service.deactivate(link.code)
    with pytest.raises(LinkGone):
        service.resolve(link.code)


class _ConstantGen(CodeGenerator):
    def __init__(self, codes):
        super().__init__(6)
        self._codes = iter(codes)

    def generate(self):
        return next(self._codes)


def test_collision_is_retried(repo, settings, clock):
    svc = ShortenerService(repo, repo, settings, codegen=_ConstantGen(["AAAAAA", "AAAAAA", "BBBBBB"]),
                           clock=clock)
    assert svc.create("https://a.com").code == "AAAAAA"
    assert svc.create("https://b.com").code == "BBBBBB"   # first attempt collided


def test_code_space_exhaustion_is_reported(repo, settings, clock):
    svc = ShortenerService(repo, repo, settings, codegen=_ConstantGen(["SAME00"] * 10), clock=clock)
    svc.create("https://a.com")
    with pytest.raises(CodeSpaceExhausted):
        svc.create("https://b.com")
