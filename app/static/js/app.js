/* Application JavaScript pour Outlook Contact Extractor */

// Configuration globale
window.ContactExtractor = {
    config: {
        apiBaseUrl: window.location.origin,
        pollingInterval: 2000,
        notificationTimeout: 5000,
        maxRetries: 3
    },
    
    state: {
        currentUser: null,
        currentSession: null,
        isExtracting: false,
        filters: {},
        pagination: { page: 1, limit: 20 }
    }
};

// Service de gestion des API
class ApiService {
    constructor(baseUrl) {
        this.baseUrl = baseUrl;
        this.setupAxios();
    }
    
    setupAxios() {
        axios.defaults.baseURL = this.baseUrl;
        
        // Interceptor pour les erreurs
        axios.interceptors.response.use(
            response => response,
            error => {
                this.handleError(error);
                return Promise.reject(error);
            }
        );
    }
    
    handleError(error) {
        console.error('API Error:', error);
        
        if (error.response) {
            const status = error.response.status;
            const message = error.response.data?.detail || 'Erreur serveur';
            
            switch (status) {
                case 401:
                    NotificationService.show('Session expirée, veuillez vous reconnecter', 'error');
                    // Redirection vers la page de connexion
                    window.location.href = '/auth/login';
                    break;
                case 403:
                    NotificationService.show('Accès non autorisé', 'error');
                    break;
                case 404:
                    NotificationService.show('Ressource non trouvée', 'error');
                    break;
                case 429:
                    NotificationService.show('Trop de requêtes, veuillez patienter', 'warning');
                    break;
                case 500:
                    NotificationService.show('Erreur interne du serveur', 'error');
                    break;
                default:
                    NotificationService.show(message, 'error');
            }
        } else if (error.request) {
            NotificationService.show('Impossible de contacter le serveur', 'error');
        } else {
            NotificationService.show('Une erreur inattendue est survenue', 'error');
        }
    }
    
    // Méthodes API
    async startExtraction(periodMonths) {
        const response = await axios.post('/api/extract', null, {
            params: { period_months: periodMonths }
        });
        return response.data;
    }
    
    async getExtractionStatus(sessionId) {
        const response = await axios.get(`/api/extraction/${sessionId}/status`);
        return response.data;
    }
    
    async getContacts(sessionId, params = {}) {
        const response = await axios.get('/api/contacts', {
            params: { session_id: sessionId, ...params }
        });
        return response.data;
    }
    
    async getStats(sessionId) {
        const response = await axios.get('/api/stats', {
            params: { session_id: sessionId }
        });
        return response.data;
    }
    
    async updateContactValidation(contactId, validated) {
        const response = await axios.put(`/api/contacts/${contactId}/validate`, null, {
            params: { validated }
        });
        return response.data;
    }
    
    async exportContacts(sessionId, validatedOnly = true) {
        const response = await axios.get('/api/contacts/export', {
            params: { 
                session_id: sessionId,
                validated_only: validatedOnly 
            },
            responseType: 'blob'
        });
        return response.data;
    }
}

// Service de notifications
class NotificationService {
    static show(message, type = 'info', timeout = 5000) {
        const alertClass = this.getAlertClass(type);
        const iconClass = this.getIconClass(type);
        
        const notification = document.createElement('div');
        notification.className = `alert ${alertClass} alert-dismissible fade show notification`;
        notification.style.cssText = 'position: fixed; top: 20px; right: 20px; z-index: 1060; min-width: 350px; max-width: 500px;';
        
        notification.innerHTML = `
            <i class="${iconClass} me-2"></i>
            <span>${message}</span>
            <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
        `;
        
        document.body.appendChild(notification);
        
        // Auto-supprimer
        if (timeout > 0) {
            setTimeout(() => {
                if (notification.parentNode) {
                    notification.remove();
                }
            }, timeout);
        }
        
        return notification;
    }
    
    static getAlertClass(type) {
        const classes = {
            success: 'alert-success',
            error: 'alert-danger',
            warning: 'alert-warning',
            info: 'alert-info'
        };
        return classes[type] || classes.info;
    }
    
    static getIconClass(type) {
        const icons = {
            success: 'fas fa-check-circle',
            error: 'fas fa-exclamation-circle',
            warning: 'fas fa-exclamation-triangle',
            info: 'fas fa-info-circle'
        };
        return icons[type] || icons.info;
    }
}

// Service de formatage et utilitaires
class UtilsService {
    static formatDate(dateString) {
        if (!dateString) return '-';
        
        const date = new Date(dateString);
        const now = new Date();
        const diffTime = Math.abs(now - date);
        const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
        
        if (diffDays === 1) {
            return 'Hier';
        } else if (diffDays < 7) {
            return `Il y a ${diffDays} jours`;
        } else {
            return date.toLocaleDateString('fr-FR', {
                year: 'numeric',
                month: 'short',
                day: 'numeric'
            });
        }
    }
    
    static formatDateTime(dateString) {
        if (!dateString) return '-';
        const date = new Date(dateString);
        return date.toLocaleDateString('fr-FR') + ' ' + date.toLocaleTimeString('fr-FR', {
            hour: '2-digit',
            minute: '2-digit'
        });
    }
    
    static formatName(contact) {
        if (contact.nom_complet) return contact.nom_complet;
        if (contact.prenom && contact.nom) return `${contact.prenom} ${contact.nom}`;
        if (contact.nom) return contact.nom;
        if (contact.prenom) return contact.prenom;
        return contact.email;
    }
    
    static getInitials(name) {
        if (!name) return '??';
        
        const words = name.split(' ').filter(word => word.length > 0);
        if (words.length === 0) return '??';
        if (words.length === 1) return words[0].substring(0, 2).toUpperCase();
        
        return (words[0][0] + words[words.length - 1][0]).toUpperCase();
    }
    
    static truncateText(text, maxLength = 50) {
        if (!text) return '';
        if (text.length <= maxLength) return text;
        return text.substring(0, maxLength - 3) + '...';
    }
    
    static validateEmail(email) {
        const re = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
        return re.test(email);
    }
    
    static debounce(func, wait) {
        let timeout;
        return function executedFunction(...args) {
            const later = () => {
                clearTimeout(timeout);
                func(...args);
            };
            clearTimeout(timeout);
            timeout = setTimeout(later, wait);
        };
    }
    
    static downloadBlob(blob, filename) {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
    }
    
    static copyToClipboard(text) {
        if (navigator.clipboard) {
            navigator.clipboard.writeText(text).then(() => {
                NotificationService.show('Copié dans le presse-papiers', 'success', 2000);
            });
        } else {
            // Fallback pour les navigateurs plus anciens
            const textArea = document.createElement('textarea');
            textArea.value = text;
            document.body.appendChild(textArea);
            textArea.select();
            document.execCommand('copy');
            document.body.removeChild(textArea);
            NotificationService.show('Copié dans le presse-papiers', 'success', 2000);
        }
    }
}

// Service de gestion du stockage local
class StorageService {
    static set(key, value) {
        try {
            localStorage.setItem(key, JSON.stringify(value));
        } catch (error) {
            console.warn('Erreur lors de la sauvegarde:', error);
        }
    }
    
    static get(key, defaultValue = null) {
        try {
            const item = localStorage.getItem(key);
            return item ? JSON.parse(item) : defaultValue;
        } catch (error) {
            console.warn('Erreur lors de la lecture:', error);
            return defaultValue;
        }
    }
    
    static remove(key) {
        try {
            localStorage.removeItem(key);
        } catch (error) {
            console.warn('Erreur lors de la suppression:', error);
        }
    }
    
    static clear() {
        try {
            localStorage.clear();
        } catch (error) {
            console.warn('Erreur lors du nettoyage:', error);
        }
    }
}

// Service de gestion des thèmes
class ThemeService {
    static init() {
        const savedTheme = StorageService.get('theme', 'light');
        this.setTheme(savedTheme);
    }
    
    static setTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        StorageService.set('theme', theme);
    }
    
    static toggleTheme() {
        const currentTheme = document.documentElement.getAttribute('data-theme');
        const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
        this.setTheme(newTheme);
    }
}

// Initialiser les services globaux
window.apiService = new ApiService(ContactExtractor.config.apiBaseUrl);
window.NotificationService = NotificationService;
window.UtilsService = UtilsService;
window.StorageService = StorageService;
window.ThemeService = ThemeService;

// Initialisation au chargement de la page
document.addEventListener('DOMContentLoaded', function() {
    // Initialiser le thème
    ThemeService.init();
    
    // Gérer les tooltips Bootstrap
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });
    
    // Gérer les popovers Bootstrap
    const popoverTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="popover"]'));
    popoverTriggerList.map(function (popoverTriggerEl) {
        return new bootstrap.Popover(popoverTriggerEl);
    });
    
    // Ajouter les event listeners globaux
    setupGlobalEventListeners();
});

function setupGlobalEventListeners() {
    // Gérer les erreurs JavaScript non capturées
    window.addEventListener('error', function(event) {
        console.error('Erreur JavaScript:', event.error);
        NotificationService.show('Une erreur inattendue est survenue', 'error');
    });
    
    // Gérer les erreurs de promesses non capturées
    window.addEventListener('unhandledrejection', function(event) {
        console.error('Promesse rejetée:', event.reason);
        NotificationService.show('Une erreur de communication est survenue', 'error');
        event.preventDefault();
    });
    
    // Gérer la perte de connexion
    window.addEventListener('online', function() {
        NotificationService.show('Connexion rétablie', 'success', 3000);
    });
    
    window.addEventListener('offline', function() {
        NotificationService.show('Connexion perdue', 'warning', 0);
    });
}