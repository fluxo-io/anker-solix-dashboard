var dagfuncs = (window.dashAgGridFunctions =
  window.dashAgGridFunctions || {});

dagfuncs.timestampComparator = function (timestampA, timestampB) {
  function parseTimestamp(value) {
    if (!value) {
      return Number.NEGATIVE_INFINITY;
    }
    return Date.parse(value.replace(" ", "T").replace(" ", ""));
  }

  return parseTimestamp(timestampA) - parseTimestamp(timestampB);
};
