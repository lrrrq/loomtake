from __future__ import annotations

import ast
from pathlib import Path
import unittest


class ImageLibraryBenchmarkTests(unittest.TestCase):
    def test_finished_request_leaves_active_set_before_sizes_can_reenter(self):
        # Exercise the real event callbacks without starting the browser or business fixture.
        source = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_image_library.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        browser_round = next(node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_browser_round")
        callbacks = [node for node in browser_round.body
            if isinstance(node, ast.FunctionDef) and node.name in {"started", "finished"}]
        state = {"active": set(), "maximum": [0], "network": []}
        exec(compile(ast.Module(body=callbacks, type_ignores=[]), str(source), "exec"), state)

        class Request:
            def __init__(self, number):
                self.url = f"http://127.0.0.1/images/default-candidates/image-{number}/preview"
                self.timing = {"responseEnd": 1}
                self.on_sizes = None

            def sizes(self):
                if self.on_sizes is not None:
                    self.on_sizes()
                return {"responseBodySize": 4}

        requests = [Request(number) for number in range(5)]
        for request in requests[:4]:
            state["started"](request)
        requests[0].on_sizes = lambda: state["started"](requests[4])
        state["finished"](requests[0])

        self.assertEqual(state["maximum"][0], 4)
        self.assertEqual(state["active"], set(requests[1:]))
        for request in requests[1:]:
            state["finished"](request)
        self.assertEqual(state["active"], set())
        self.assertEqual(len(state["network"]), 5)


if __name__ == "__main__":
    unittest.main()
