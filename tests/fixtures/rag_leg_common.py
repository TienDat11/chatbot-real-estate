"""Test-only fakes shared by test_rag_leg_* shards."""



class FakeRag:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    async def aquery_data(self, query, param):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
