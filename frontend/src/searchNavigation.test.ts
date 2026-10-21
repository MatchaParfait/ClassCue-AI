import { expect, it } from "vitest";
import { parseSearchSeek } from "./searchNavigation";

it("accepts zero and independent navigation tokens for the same hit", () => {
  expect(parseSearchSeek("?start_ms=0&library_id=a", "one", "a")).toEqual({ token: "one", seconds: 0 });
  expect(parseSearchSeek("?start_ms=8500&library_id=a", "two", "a")).toEqual({ token: "two", seconds: 8.5 });
});
it.each(["", "?start_ms=-1", "?start_ms=NaN", "?start_ms=Infinity", "?start_ms=9007199254740992", "?start_ms=1.5"])("ignores invalid navigation %s", (value) => {
  expect(parseSearchSeek(`${value}&library_id=a`, "one", "a")).toBeUndefined();
});
it("ignores another library's search link", () => {
  expect(parseSearchSeek("?start_ms=5000&library_id=a", "one", "b")).toBeUndefined();
});
