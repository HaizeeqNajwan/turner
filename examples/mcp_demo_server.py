"""A tiny MCP server to show (and test) how external tools plug into Turner.

Enable it by copying mcp.json.example to mcp.json.
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")

UNITS = {("km", "mi"): 0.621371, ("mi", "km"): 1.609344, ("m", "ft"): 3.28084, ("ft", "m"): 0.3048,
         ("m", "km"): 0.001, ("km", "m"): 1000.0, ("m", "mi"): 0.000621371, ("mi", "m"): 1609.344,
         ("kg", "lb"): 2.204623, ("lb", "kg"): 0.453592, ("ha", "acre"): 2.471054, ("acre", "ha"): 0.404686}


@mcp.tool()
def convert_units(value: float, from_unit: str, to_unit: str) -> str:
    """Convert a value between units: m, km, mi, ft, kg/lb, ha/acre."""
    key = (from_unit.strip().lower(), to_unit.strip().lower())
    if key not in UNITS:
        return "unsupported conversion %s -> %s" % key
    return "%g %s = %.4f %s" % (value, key[0], value * UNITS[key], key[1])


@mcp.tool()
def chainage_length(start: str, end: str) -> str:
    """Length in metres between two road chainages written like CH033+475 and CH038+475."""
    def metres(ch):
        km, _, m = ch.upper().replace("CH", "").strip().partition("+")
        return int(km) * 1000 + float(m or 0)
    return "%.1f m" % (metres(end) - metres(start))


if __name__ == "__main__":
    mcp.run()
