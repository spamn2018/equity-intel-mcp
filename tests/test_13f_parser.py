"""Tests for the 13F-HR XML information-table parser."""
from __future__ import annotations

import pytest

from equity_intel.sec.parser_13f import (
    compute_holding_changes,
    parse_13f_header,
    parse_13f_information_table,
)

# ---------------------------------------------------------------------------
# Fixtures: sample XML strings
# ---------------------------------------------------------------------------

SIMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=13F-HR">
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>12345678</value>
    <shrsOrPrnAmt>
      <sshPrnamt>5000000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <putCall></putCall>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <otherManager></otherManager>
    <votingAuthority>
      <Sole>5000000</Sole>
      <Shared>0</Shared>
      <None>0</None>
    </votingAuthority>
  </infoTable>
  <infoTable>
    <nameOfIssuer>MICROSOFT CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>594918104</cusip>
    <value>9876543</value>
    <shrsOrPrnAmt>
      <sshPrnamt>2500000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <putCall></putCall>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority>
      <Sole>2500000</Sole>
      <Shared>0</Shared>
      <None>0</None>
    </votingAuthority>
  </infoTable>
</informationTable>
"""

# XML with a namespace prefix (older filer variant)
PREFIXED_NS_XML = """<?xml version="1.0"?>
<ns0:informationTable xmlns:ns0="http://www.sec.gov/cgi-bin/browse-edgar">
  <ns0:infoTable>
    <ns0:nameOfIssuer>NVIDIA CORP</ns0:nameOfIssuer>
    <ns0:titleOfClass>COM</ns0:titleOfClass>
    <ns0:cusip>67066G104</ns0:cusip>
    <ns0:value>5000000</ns0:value>
    <ns0:shrsOrPrnAmt>
      <ns0:sshPrnamt>1000000</ns0:sshPrnamt>
      <ns0:sshPrnamtType>SH</ns0:sshPrnamtType>
    </ns0:shrsOrPrnAmt>
    <ns0:putCall></ns0:putCall>
    <ns0:investmentDiscretion>SOLE</ns0:investmentDiscretion>
    <ns0:votingAuthority>
      <ns0:Sole>1000000</ns0:Sole>
      <ns0:Shared>0</ns0:Shared>
      <ns0:None>0</ns0:None>
    </ns0:votingAuthority>
  </ns0:infoTable>
</ns0:informationTable>
"""

# Put/call option row
OPTIONS_XML = """<?xml version="1.0"?>
<informationTable>
  <infoTable>
    <nameOfIssuer>TESLA INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>88160R101</cusip>
    <value>500000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>100000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <putCall>Put</putCall>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority>
      <Sole>0</Sole>
      <Shared>0</Shared>
      <None>100000</None>
    </votingAuthority>
  </infoTable>
</informationTable>
"""

# Value with comma formatting (some filers)
COMMA_VALUE_XML = """<?xml version="1.0"?>
<informationTable>
  <infoTable>
    <nameOfIssuer>AMAZON COM INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>023135106</cusip>
    <value>1,234,567</value>
    <shrsOrPrnAmt>
      <sshPrnamt>1,000,000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <putCall></putCall>
    <investmentDiscretion>SHARED</investmentDiscretion>
    <votingAuthority>
      <Sole>0</Sole>
      <Shared>1000000</Shared>
      <None>0</None>
    </votingAuthority>
  </infoTable>
</informationTable>
"""

SAMPLE_HEADER = """
COMPANY CONFORMED NAME: BERKSHIRE HATHAWAY INC
CENTRAL INDEX KEY: 0001067983
PERIOD OF REPORT: 20231231
"""


# ---------------------------------------------------------------------------
# parse_13f_information_table
# ---------------------------------------------------------------------------

class TestParse13fInformationTable:

    def test_basic_two_holdings(self):
        holdings = parse_13f_information_table(SIMPLE_XML)
        assert len(holdings) == 2

    def test_first_holding_fields(self):
        holdings = parse_13f_information_table(SIMPLE_XML)
        h = holdings[0]
        assert h["issuer_name"] == "APPLE INC"
        assert h["cusip"] == "037833100"
        assert h["title_of_class"] == "COM"
        assert h["value_usd"] == 12345678
        assert h["shares"] == 5000000
        assert h["share_type"] == "SH"
        assert h["put_call"] is None
        assert h["investment_discretion"] == "SOLE"

    def test_second_holding_fields(self):
        holdings = parse_13f_information_table(SIMPLE_XML)
        h = holdings[1]
        assert h["issuer_name"] == "MICROSOFT CORP"
        assert h["cusip"] == "594918104"
        assert h["shares"] == 2500000

    def test_namespace_prefix_stripped(self):
        """Namespace-prefixed tags should be parsed identically."""
        holdings = parse_13f_information_table(PREFIXED_NS_XML)
        assert len(holdings) == 1
        assert holdings[0]["issuer_name"] == "NVIDIA CORP"
        assert holdings[0]["cusip"] == "67066G104"
        assert holdings[0]["shares"] == 1000000

    def test_put_call_preserved(self):
        holdings = parse_13f_information_table(OPTIONS_XML)
        assert holdings[0]["put_call"] == "Put"

    def test_comma_formatted_values(self):
        holdings = parse_13f_information_table(COMMA_VALUE_XML)
        assert holdings[0]["value_usd"] == 1234567
        assert holdings[0]["shares"] == 1000000

    def test_shared_discretion(self):
        holdings = parse_13f_information_table(COMMA_VALUE_XML)
        assert holdings[0]["investment_discretion"] == "SHARED"

    def test_empty_xml_returns_empty_list(self):
        assert parse_13f_information_table("") == []

    def test_table_with_no_rows(self):
        xml = "<informationTable></informationTable>"
        assert parse_13f_information_table(xml) == []

    def test_invalid_xml_raises_value_error(self):
        with pytest.raises(ValueError, match="Failed to parse"):
            parse_13f_information_table("<unclosed>")

    def test_cusip_normalized_uppercase(self):
        holdings = parse_13f_information_table(SIMPLE_XML)
        assert holdings[0]["cusip"] == holdings[0]["cusip"].upper()

    def test_raw_json_present(self):
        holdings = parse_13f_information_table(SIMPLE_XML)
        assert "raw_json" in holdings[0]
        assert holdings[0]["raw_json"]["issuer_name"] == "APPLE INC"


# ---------------------------------------------------------------------------
# parse_13f_header
# ---------------------------------------------------------------------------

class TestParse13fHeader:

    def test_manager_name_extracted(self):
        result = parse_13f_header(SAMPLE_HEADER)
        assert result["manager_name"] == "BERKSHIRE HATHAWAY INC"

    def test_cik_extracted_and_padded(self):
        result = parse_13f_header(SAMPLE_HEADER)
        assert result["manager_cik"] == "0001067983"

    def test_report_period_converted(self):
        result = parse_13f_header(SAMPLE_HEADER)
        assert result["report_period"] == "2023-12-31"

    def test_missing_fields_are_none(self):
        result = parse_13f_header("nothing useful here")
        assert result["manager_name"] is None
        assert result["manager_cik"] is None
        assert result["report_period"] is None

    def test_iso_period_preserved(self):
        header = "PERIOD OF REPORT: 2024-03-31"
        result = parse_13f_header(header)
        assert result["report_period"] == "2024-03-31"


# ---------------------------------------------------------------------------
# compute_holding_changes
# ---------------------------------------------------------------------------

class TestComputeHoldingChanges:

    def _h(self, cusip, issuer, shares, value=0):
        return {
            "cusip": cusip,
            "issuer_name": issuer,
            "shares": shares,
            "value_usd": value,
            "share_type": "SH",
        }

    def test_new_position_detected(self):
        prev = []
        curr = [self._h("037833100", "APPLE INC", 1_000_000)]
        changes = compute_holding_changes(prev, curr)
        assert len(changes) == 1
        assert changes[0]["change_type"] == "new_position"
        assert changes[0]["cusip"] == "037833100"
        assert changes[0]["prev_shares"] is None
        assert changes[0]["curr_shares"] == 1_000_000

    def test_exit_position_detected(self):
        prev = [self._h("037833100", "APPLE INC", 1_000_000)]
        curr = []
        changes = compute_holding_changes(prev, curr)
        assert len(changes) == 1
        assert changes[0]["change_type"] == "exit_position"
        assert changes[0]["curr_shares"] is None

    def test_major_increase_detected(self):
        prev = [self._h("037833100", "APPLE INC", 1_000_000)]
        curr = [self._h("037833100", "APPLE INC", 2_000_000)]
        changes = compute_holding_changes(prev, curr, change_threshold_pct=10.0)
        assert len(changes) == 1
        assert changes[0]["change_type"] == "major_increase"
        assert changes[0]["pct_change"] == pytest.approx(100.0)

    def test_major_decrease_detected(self):
        prev = [self._h("037833100", "APPLE INC", 1_000_000)]
        curr = [self._h("037833100", "APPLE INC", 500_000)]
        changes = compute_holding_changes(prev, curr, change_threshold_pct=10.0)
        assert len(changes) == 1
        assert changes[0]["change_type"] == "major_decrease"
        assert changes[0]["pct_change"] == pytest.approx(-50.0)

    def test_small_change_ignored(self):
        """Changes below the threshold produce no change record."""
        prev = [self._h("037833100", "APPLE INC", 1_000_000)]
        curr = [self._h("037833100", "APPLE INC", 1_050_000)]  # +5%
        changes = compute_holding_changes(prev, curr, change_threshold_pct=10.0)
        assert changes == []

    def test_unchanged_position_no_change(self):
        prev = [self._h("037833100", "APPLE INC", 1_000_000)]
        curr = [self._h("037833100", "APPLE INC", 1_000_000)]
        changes = compute_holding_changes(prev, curr)
        assert changes == []

    def test_multiple_changes(self):
        prev = [
            self._h("037833100", "APPLE INC", 1_000_000),
            self._h("594918104", "MICROSOFT CORP", 500_000),
        ]
        curr = [
            self._h("037833100", "APPLE INC", 2_000_000),  # increase
            self._h("67066G104", "NVIDIA CORP", 300_000),   # new
            # Microsoft exited
        ]
        changes = compute_holding_changes(prev, curr, change_threshold_pct=10.0)
        change_types = {c["change_type"] for c in changes}
        assert "new_position" in change_types
        assert "exit_position" in change_types
        assert "major_increase" in change_types

    def test_both_empty_no_changes(self):
        assert compute_holding_changes([], []) == []

    def test_pct_change_none_for_new_and_exit(self):
        prev = []
        curr = [self._h("037833100", "APPLE INC", 1_000_000)]
        changes = compute_holding_changes(prev, curr)
        assert changes[0]["pct_change"] is None
