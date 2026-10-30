"""Produtos vendidos nas lanchonetes da casa, informados pela comunidade (nome e categoria).

Entram no banco sem preço e inativos; ficam disponíveis depois que o preço é preenchido em
Cadastros > Preços em lista.
"""
PRODUTOS_CASA = {
    "Bebida não alcoólica": [
        "Água Staff", "Água Copo", "Água De Coco", "Água Tônica", "Pepsi", "Pepsi Black", "Guaraná Antarctica",
        "H2O Limoneto 500ml", "Suco de Uva", "Red Bull Regular", "Red Bull Zero", "Red Bull Tropical", "Gatorade",
        "Heineken 0", "Cerveja sem Álcool", "Budweiser 0.0", "Chá Lipton",
    ],
    "Bebida alcoólica": [
        "Amstel", "Heineken", "Heineken Ultimate", "Eisenbahn IPA", "Eisenbahn Pilsen", "Skol Beats Red Mix",
        "Skol Beats Senses", "Budweiser", "Smirnoff Ice", "Smirnoff Green Apple", "Brutal Fruit", "Aperol", "Gin Gordons",
        "Gin Tanqueray", "Vodka Ciroc", "Vodka Ketel One", "Whisky", "Vinho Branco", "Vinho Tinto",
    ],
    "Café": [
        "Café",
    ],
    "Lanche": [
        "Vegetal Burguer", "Gourmet Burguer", "Gourmet Hot Dog", "Pernil em Lascas", "Lasanha Bolonhesa",
        "Pizza Calabresa", "Pizza Mussarela", "Pizza Inteira", "Sanduíche de Linguiça", "Dog + Guaraná", "Burguer Bacon",
    ],
    "Snack": [
        "Pipoca", "Batata Lays", "Batata Lays Picanha", "Batata Lays Queijo", "Batata Lays Taco Mex", "Doritos",
        "Doritos Spicy", "Popcorners", "Torcida", "Cheetos",
    ],
    "Doce": [
        "Cookie Original", "Cookie Chocolate", "Bolinho Duplo Chocolate", "Bolinho Gotas de Chocolate",
        "Bolinho Laranja", "Sorvete Brigadeiro", "Sorvete Coco Branco", "Sorvete Coco Branco Zero", "Sorvete Limão",
        "Sorvete Milho", "Sorvete Tangerina", "Sorvete Pistache", "Sorvete Uva",
    ],
}
