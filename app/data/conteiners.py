from dataclasses import dataclass


@dataclass
class Conteiner:
    nome: str
    cx: int       # comprimento em cm
    cy: int       # largura em cm
    cz: int       # altura em cm
    peso_max: int # limite de peso em gramas
    vol_max: int  # limite de volume em cm³


CONTEINERES = {
    "20ft": Conteiner("20ft Standard",   589, 235, 239, 21_770_000,  33_000_000),
    "40ft": Conteiner("40ft Standard",  1203, 235, 239, 26_680_000,  67_000_000),
    "40hc": Conteiner("40ft High Cube", 1203, 235, 269, 28_600_000,  76_000_000),
    "45hc": Conteiner("45ft High Cube", 1356, 235, 269, 27_600_000,  86_000_000),
}


def conteiner_personalizado(cx: int, cy: int, cz: int, peso_max_kg: float, vol_max_m3: float) -> Conteiner:
    return Conteiner(
        nome="Personalizado",
        cx=cx, cy=cy, cz=cz,
        peso_max=int(peso_max_kg * 1000),
        vol_max=int(vol_max_m3 * 1_000_000),
    )
