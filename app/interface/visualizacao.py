import pyvista as pv
import numpy as np
import matplotlib.pyplot as plt
from app.data.conteiners import Conteiner


def _bounds_caixa(item: dict, itens_dados: dict) -> tuple:
    x, y, z = item["st_x"], item["st_y"], item["st_z"]
    dx, dy  = item["dx"], item["dy"]
    # Altura ORIENTADA (a caixa pode tombar); fallback ao envelope p/ entradas antigas
    dz      = item.get("dz", itens_dados[item["nome"]]["z"])
    return (x, x + dx, y, y + dy, z, z + dz)


def _rgb_de_mpl(cor_mpl) -> list:
    r, g, b = cor_mpl[:3]
    return [int(r * 255), int(g * 255), int(b * 255)]


def visualizar_carregamento(lista_carregamento: list, itens_dados: dict, conteiner: Conteiner) -> None:
    cores_mpl = plt.cm.Set2.colors
    plotter   = pv.Plotter(window_size=[1400, 900])
    plotter.set_background("white")

    # Wireframe do contêiner
    box_conteiner = pv.Box(bounds=(0, conteiner.cx, 0, conteiner.cy, 0, conteiner.cz))
    plotter.add_mesh(
        box_conteiner.extract_all_edges(),
        color="gray", line_width=1.5, opacity=0.6, style="wireframe",
    )

    centros_label    = []
    nomes_label      = []
    entradas_legenda = []

    for i, item in enumerate(lista_carregamento):
        cor     = cores_mpl[i % len(cores_mpl)]
        cor_rgb = _rgb_de_mpl(cor)
        bounds  = _bounds_caixa(item, itens_dados)

        plotter.add_mesh(
            pv.Box(bounds=bounds),
            color=cor_rgb, opacity=0.45,
            show_edges=True, edge_color="black", line_width=0.8,
        )

        cx = (bounds[0] + bounds[1]) / 2
        cy = (bounds[2] + bounds[3]) / 2
        cz = (bounds[4] + bounds[5]) / 2
        centros_label.append([cx, cy, cz])
        nomes_label.append(item["nome"])

        seq = item.get("sequencia", 1 + i)
        entradas_legenda.append((f"{seq}º {item['nome']}", [c / 255 for c in cor_rgb]))

    plotter.add_point_labels(
        np.array(centros_label), nomes_label,
        font_size=10, bold=True, text_color="white",
        always_visible=True, show_points=False, shape_opacity=0.0,
    )
    plotter.add_legend(
        entradas_legenda,
        loc="upper left", size=(0.8, 0.8),
        bcolor="black", border=True, background_opacity=0.5,
    )
    plotter.show_axes()
    plotter.camera.elevation = 0
    plotter.camera.azimuth   = 0
    plotter.camera.zoom(0.6)
    plotter.show(title=f"Carregamento — {conteiner.nome}")
