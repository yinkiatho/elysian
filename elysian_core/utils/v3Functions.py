from decimal import Decimal
import math

ZERO = Decimal(0)
ONE = Decimal(1)
TWO = Decimal(2)
U64_MAX = (TWO ** 64) - ONE


class ClmmpoolsError(Exception):
    """Custom exception for Clmmpools errors."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class MathErrorCode:
    DivideByZero = "DivideByZero"
    MulDivOverflow = "MulDivOverflow"
    UnsignedIntegerOverflow = "UnsignedIntegerOverflow"
    MulShiftLeftOverflow = "MulShiftLeftOverflow"
    MulOverflow = "MulOverflow"
    
    

def amount_to_base(string_amount, decimals):
    return float(int(string_amount) / (10**decimals))



def calculate_price_from_sqrt_price(sqrt_price_x_64: str, decimals1: int, decimals2: int) -> float:
    """
    calculate the price for a given sqrt price for a base asset in terms of a pair asset
    :param sqrt_price_x_64: integer amount for sqrt price of asset 1 in terms of asset 2
    :param asset_1: str for the base asset
    :param asset_2: str for the pair asset
    :return: float amount for the price
    """
    price = ((float(sqrt_price_x_64) * (2**-64)) ** 2) * (10 **(decimals1-decimals2))
    return price



def calculate_price_from_sqrt_price2(sqrt_price_x_64: str, decimals1: int, decimals2: int) -> float:
    """
    calculate the price for a given sqrt price for a base asset in terms of a pair asset
    :param sqrt_price_x_64: integer amount for sqrt price of asset 1 in terms of asset 2
    :param asset_1: str for the base asset
    :param asset_2: str for the pair asset
    :return: float amount for the price
    """
    price = ((float(sqrt_price_x_64) * (2**-64)) ** 2) * (10 **(decimals2 - decimals1))
    return price

