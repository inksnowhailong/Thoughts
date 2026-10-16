from guanjia.p3_owner_binding import build_patch


def check(value, label):
    print(("PASS " if value else "FAIL ") + label)
    if not value:
        raise SystemExit(1)


patch = build_patch("owner-account", "OwnerExact@im.wechat")
binding = patch["bindings"][0]
check(binding["session"]["dmScope"] == "main" and binding["match"]["accountId"] == "owner-account", "owner binding enters main session")
check(patch["commands"]["ownerAllowFrom"] == ["OwnerExact@im.wechat"], "owner id remains explicit and case-preserved")
try:
    build_patch("", "")
except ValueError:
    check(True, "missing owner identity fails closed")
else:
    check(False, "missing owner identity fails closed")
print("ALL PASS test_p3_owner_binding")
