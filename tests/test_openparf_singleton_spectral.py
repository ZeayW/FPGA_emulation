import os
import unittest

IMPORT_ERROR = None
try:
    import torch
    from openparf.ops.dct import dct
    from openparf.ops.electric_potential.electric_potential import (
        _DirectSpectralTransform2D,
    )
except (ImportError, ModuleNotFoundError) as error:
    IMPORT_ERROR = error
    torch = None
    dct = None
    _DirectSpectralTransform2D = None

if os.environ.get("EMUFLOW_REQUIRE_OPENPARF_TEST") == "1" and IMPORT_ERROR:
    raise IMPORT_ERROR


@unittest.skipIf(IMPORT_ERROR is not None, "root-built OpenPARF is unavailable")
class OpenParfSingletonSpectralTest(unittest.TestCase):
    def test_direct_transform_matches_native_conventions(self) -> None:
        value = torch.arange(1, 9, dtype=torch.float64).reshape(4, 2)
        direct = _DirectSpectralTransform2D(4, 2, value.dtype, value.device)
        coefficients = dct.Dct2()(value).clone()

        self.assertTrue(torch.allclose(direct.dct2(value), coefficients))
        self.assertTrue(
            torch.allclose(
                direct.idct2(coefficients),
                dct.Idct2()(coefficients).clone(),
            )
        )
        self.assertTrue(
            torch.allclose(
                direct.idxst_idct(coefficients),
                dct.IdxstIdct()(coefficients).clone(),
            )
        )
        self.assertTrue(
            torch.allclose(
                direct.idct_idxst(coefficients),
                dct.IdctIdxst()(coefficients).clone(),
            )
        )

    def test_singleton_dimension_is_finite_and_invertible(self) -> None:
        value = torch.tensor([[0.25], [0.75]], dtype=torch.float64)
        direct = _DirectSpectralTransform2D(2, 1, value.dtype, value.device)
        coefficients = direct.dct2(value)

        self.assertTrue(torch.isfinite(coefficients).all())
        self.assertTrue(
            torch.allclose(direct.idct2(coefficients), 4.0 * value)
        )
        self.assertTrue(torch.isfinite(direct.idxst_idct(coefficients)).all())
        self.assertTrue(
            torch.equal(direct.idct_idxst(coefficients), torch.zeros_like(value))
        )


if __name__ == "__main__":
    unittest.main()
