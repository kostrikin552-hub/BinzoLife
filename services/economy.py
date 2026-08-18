def calculate_potential_saving(price: float, alternative_price: float, tank_volume: float = 50.0) -> float:
    if price <= 0 or alternative_price <= 0:
        return 0.0
    diff = alternative_price - price
    if diff <= 0:
        return 0.0
    return round(diff * tank_volume, 2)
