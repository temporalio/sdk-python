import temporalio.nexus._link_conversion


def test_link_conversion_utilities():
    p2c = temporalio.nexus._link_conversion._event_type_pascal_case_to_constant_case
    c2p = temporalio.nexus._link_conversion._event_type_constant_case_to_pascal_case

    for p, c in [
        ("", ""),
        ("A", "A"),
        ("Ab", "AB"),
        ("AbCd", "AB_CD"),
        ("AbCddE", "AB_CDD_E"),
        ("ContainsAOneLetterWord", "CONTAINS_A_ONE_LETTER_WORD"),
        ("NexusOperationScheduled", "NEXUS_OPERATION_SCHEDULED"),
    ]:
        assert p2c(p) == c
        assert c2p(c) == p

    assert p2c("a") == "A"
    assert c2p("A") == "A"
