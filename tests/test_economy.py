from services.economy import calculate_potential_saving

def test_economy():
    saving = calculate_potential_saving(65.0, 67.0, 50)
    assert saving == 100.0
    saving_zero = calculate_potential_saving(67.0, 65.0, 50)
    assert saving_zero == 0.0
