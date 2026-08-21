import sys
sys.path.insert(0, '.')
import unittest
from tests.test_assets_contract import AssetsContractTest

suite = unittest.TestLoader().loadTestsFromTestCase(AssetsContractTest)
runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(suite)
print("Contract test success:", result.wasSuccessful())
