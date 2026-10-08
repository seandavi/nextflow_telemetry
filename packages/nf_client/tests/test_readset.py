"""Readset ids: ADR-0007's golden vectors and the seqcol spec examples it cites.

cf/test/readset.test.ts pins the control plane's TypeScript copy to the same values.
"""
import pytest

from nf_client.readset import canonical_json, insdc_unit, readset_id, sha512t24u

ZELLER = [
    insdc_unit(r)
    for r in ["ERR478958", "ERR478959", "ERR478960", "ERR478961", "ERR480454", "ERR480455", "ERR480456", "ERR480457"]
]


def test_seqcol_spec_examples():
    assert sha512t24u(canonical_json(["chr1", "chr2", "chr3"])) == "g04lKdxiYtG3dOGeUC5AdKEifw65G0Wp"
    level1 = {"sequences": "rD29ZKmEqwwHRXjiQ36p6UMZQ5hemmsb", "names": "g04lKdxiYtG3dOGeUC5AdKEifw65G0Wp"}
    assert sha512t24u(canonical_json(level1)) == "sjNNwm4zov3Dl0FRWbRTcZwzqrTQKIqL"


@pytest.mark.parametrize(
    ("units", "units_digest", "rs_id"),
    [
        (["insdc.sra:SRR000001"], "YT21j4gzsIn8wOjEyeLa3MLDtgDplqBj", "RS.29BkNp8wxCWwuhVe3luQxYtv97BwdwjF"),
        (ZELLER, "dPkNQbShezkLh6trclk7YEYbYPskOOfP", "RS.l29A5uBFtCKLgc-EPvUhj0hD6Q02Z7qj"),
        (
            [ZELLER[i] for i in (5, 0, 7, 2, 1, 6, 3, 4, 0)],
            "dPkNQbShezkLh6trclk7YEYbYPskOOfP",
            "RS.l29A5uBFtCKLgc-EPvUhj0hD6Q02Z7qj",
        ),
        (
            ["insdc.sra:SRR1", "insdc.sra:ERR2", "insdc.sra:DRR3"],
            "SiHrRpm6pYqQOnfGN-6yg_ZssJWLjjGP",
            "RS.uFRI93utvCk3llTO2fQPqARuz-e6Lj_-",
        ),
    ],
)
def test_adr_0007_golden_vectors(units, units_digest, rs_id):
    assert sha512t24u(canonical_json(sorted(set(units)))) == units_digest
    assert readset_id(units) == rs_id


def test_errors():
    with pytest.raises(ValueError, match="no units"):
        readset_id([])
    with pytest.raises(ValueError, match="invalid readset unit"):
        readset_id(["SRR1"])
    with pytest.raises(ValueError, match="not an INSDC run accession"):
        insdc_unit("SRS123")
    assert insdc_unit(" SRR7 ") == "insdc.sra:SRR7"
