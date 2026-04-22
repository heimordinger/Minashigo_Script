# core/coord/viewport_context.py

class ViewportContext:
    def __init__(self):
        self._dprs: dict[str, float] = {}

    def _account_key(self, account: dict | str) -> str:
        if isinstance(account, dict):
            try:
                return account["email"]
            except KeyError:
                raise ValueError("account dict missing 'email'")
        elif isinstance(account, str):
            return account
        else:
            raise TypeError("account must be dict or str")

    def add_for_account(self, *, account: dict | str, dpr: float):
        key = self._account_key(account)
        self._dprs[key] = float(dpr)

    def get_dpr(self, *, account: dict | str) -> float:
        key = self._account_key(account)
        return self._dprs.get(key, 1.0)


viewport_ctx = ViewportContext()
