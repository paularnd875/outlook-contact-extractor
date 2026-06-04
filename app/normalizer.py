import unicodedata
import re
from typing import Optional

def normalize_name(name: Optional[str]) -> Optional[str]:
    """
    Normaliser un nom selon la logique fournie :
    - Convertir en minuscules
    - Supprimer les accents
    - Garder uniquement lettres et chiffres
    
    Args:
        name: Le nom à normaliser
        
    Returns:
        str: Le nom normalisé ou None si entrée vide
    """
    
    if not name:
        return None
    
    # Convertir en string et en minuscules
    normalized = str(name).lower()
    
    # Supprimer les accents (NFD normalization + suppression des diacritiques)
    normalized = unicodedata.normalize('NFD', normalized)
    normalized = ''.join(
        char for char in normalized 
        if unicodedata.category(char) != 'Mn'  # Mn = Nonspacing_Mark (accents)
    )
    
    # Garder uniquement les lettres et chiffres
    normalized = re.sub(r'[^a-z0-9]', '', normalized)
    
    # Retourner None si la chaîne devient vide
    return normalized if normalized else None

def create_normalized_full_name(nom: Optional[str], prenom: Optional[str]) -> Optional[str]:
    """
    Créer un nom complet normalisé à partir du nom et prénom
    
    Args:
        nom: Nom de famille
        prenom: Prénom
        
    Returns:
        str: Nom complet normalisé (prenomnom) ou None
    """
    
    nom_norm = normalize_name(nom) if nom else ""
    prenom_norm = normalize_name(prenom) if prenom else ""
    
    # Concatener prenom + nom
    full_normalized = f"{prenom_norm}{nom_norm}"
    
    return full_normalized if full_normalized else None

def create_normalized_display_name(display_name: Optional[str]) -> Optional[str]:
    """
    Normaliser un nom d'affichage complet
    
    Args:
        display_name: Nom complet à normaliser
        
    Returns:
        str: Nom normalisé ou None
    """
    
    return normalize_name(display_name)

def smart_normalize_contact(nom: Optional[str], prenom: Optional[str], nom_complet: Optional[str]) -> Optional[str]:
    """
    Normalisation intelligente d'un contact en utilisant toutes les informations disponibles
    
    Args:
        nom: Nom de famille
        prenom: Prénom  
        nom_complet: Nom complet d'affichage
        
    Returns:
        str: Meilleure normalisation possible
    """
    
    # Méthode 1: Si on a nom ET prénom, utiliser prenomnom
    if nom and prenom:
        return create_normalized_full_name(nom, prenom)
    
    # Méthode 2: Si on a un nom complet, le normaliser directement
    if nom_complet:
        return create_normalized_display_name(nom_complet)
    
    # Méthode 3: Si on a seulement le nom OU le prénom
    if nom:
        return normalize_name(nom)
    
    if prenom:
        return normalize_name(prenom)
    
    # Aucune information utilisable
    return None

# Fonctions utilitaires pour les tests et validation

def test_normalizer():
    """Tester la fonction de normalisation avec des exemples"""
    
    test_cases = [
        ("Jean-Claude", "jeanclaude"),
        ("Marie-Ève", "marieve"),
        ("François", "francois"),
        ("José María", "josemaria"),
        ("O'Connor", "oconnor"),
        ("McDonald's", "mcdonalds"),
        ("123 Test", "123test"),
        ("", None),
        (None, None),
        ("Dupont-Durand", "dupontdurand")
    ]
    
    print("Test de la normalisation:")
    for input_name, expected in test_cases:
        result = normalize_name(input_name)
        status = "✅" if result == expected else "❌"
        print(f"{status} '{input_name}' -> '{result}' (attendu: '{expected}')")

def test_full_name_normalization():
    """Tester la normalisation de noms complets"""
    
    test_cases = [
        ("Dupont", "Jean", "jeandupont"),
        ("Martin", "Marie-Claire", "marieclairemartin"),
        ("O'Connor", "Patrick", "patrickoconnor"),
        ("", "Jean", "jean"),
        ("Dupont", "", "dupont"),
        ("", "", None),
        (None, None, None)
    ]
    
    print("\nTest de la normalisation nom complet:")
    for nom, prenom, expected in test_cases:
        result = create_normalized_full_name(nom, prenom)
        status = "✅" if result == expected else "❌"
        print(f"{status} '{prenom}' + '{nom}' -> '{result}' (attendu: '{expected}')")

def test_smart_normalization():
    """Tester la normalisation intelligente"""
    
    test_cases = [
        ("Dupont", "Jean", "Jean Dupont", "jeandupont"),  # Nom + Prénom disponibles
        (None, None, "Marie Martin", "mariemartin"),      # Seulement nom complet
        ("Dupont", None, None, "dupont"),                 # Seulement nom
        (None, "Jean", None, "jean"),                     # Seulement prénom
        (None, None, None, None)                          # Rien
    ]
    
    print("\nTest de la normalisation intelligente:")
    for nom, prenom, nom_complet, expected in test_cases:
        result = smart_normalize_contact(nom, prenom, nom_complet)
        status = "✅" if result == expected else "❌"
        print(f"{status} Nom:'{nom}' Prénom:'{prenom}' Complet:'{nom_complet}' -> '{result}' (attendu: '{expected}')")

if __name__ == "__main__":
    test_normalizer()
    test_full_name_normalization() 
    test_smart_normalization()